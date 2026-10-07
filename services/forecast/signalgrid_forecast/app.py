"""NATS wiring for the forecast service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from pydantic import ValidationError

from .config import Settings
from .models import AlertMessage, RawEvent, Stats, alert_subject, forecast_subject, regime_subject
from .regime import fit_msvar
from .service import ForecastService

logger = logging.getLogger("forecast")

FORECASTS = Counter("forecast_published_total", "Forecast messages published")
REGIME_MSGS = Counter("regime_published_total", "Regime messages published")
INVALID = Counter("forecast_invalid_messages_total", "Messages that failed validation", ["kind"])
MASE = Gauge("forecast_mase", "Rolling MASE vs. persistence", ["station", "sensor", "horizon"])
ALERTS = Counter("alerts_total", "Alerts raised", ["rule", "severity", "state"])
COVERAGE = Gauge(
    "forecast_interval_coverage", "Rolling empirical interval coverage", ["station", "sensor", "horizon"]
)
REGIME_ACCURACY = Gauge("regime_accuracy", "Rolling regime detection accuracy", ["station"])
FIT_SECONDS = Histogram("regime_fit_seconds", "EM refit duration", buckets=(0.25, 0.5, 1, 2, 4, 8, 16))


def _lower_priority() -> None:
    """Refits are background work: yield the CPU to latency-sensitive services."""
    if hasattr(os, "nice"):
        os.nice(10)


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = ForecastService(settings)
        self.nc = NATS()
        # EM fits are CPU-bound pure Python/numpy loops; running them in a
        # separate process keeps them from stalling the event loop (GIL).
        self._pool = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"), initializer=_lower_priority
        )
        self._fitting: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def _on_stats(self, msg: Msg) -> None:
        try:
            stats = Stats.model_validate_json(msg.data)
        except ValidationError as exc:
            INVALID.labels("stats").inc()
            logger.warning("event=invalid_stats subject=%s error=%s", msg.subject, exc)
            return
        forecast, alerts = self.service.handle_stats(stats)
        await self._publish_alerts(alerts)
        if forecast is None:
            return
        await self.nc.publish(
            forecast_subject(forecast.station, forecast.sensor),
            forecast.model_dump_json().encode(),
        )
        FORECASTS.inc()
        for m in forecast.metrics:
            labels = (forecast.station, str(forecast.sensor), str(m.horizon))
            if m.mase is not None:
                MASE.labels(*labels).set(m.mase)
            if m.coverage is not None:
                COVERAGE.labels(*labels).set(m.coverage)

    async def _on_raw(self, msg: Msg) -> None:
        try:
            raw = RawEvent.model_validate_json(msg.data)
        except ValidationError as exc:
            INVALID.labels("raw").inc()
            logger.warning("event=invalid_raw subject=%s error=%s", msg.subject, exc)
            return

        regime, alerts = self.service.handle_raw(raw)
        await self._publish_alerts(alerts)
        if regime is not None:
            await self.nc.publish(regime_subject(regime.station), regime.model_dump_json().encode())
            REGIME_MSGS.inc()
            if regime.accuracy is not None:
                REGIME_ACCURACY.labels(regime.station).set(regime.accuracy)

        if raw.station not in self._fitting and self.service.detector(raw.station).needs_refit:
            self._fitting.add(raw.station)
            task = asyncio.create_task(self._refit(raw.station))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _publish_alerts(self, alerts: list[AlertMessage]) -> None:
        for alert in alerts:
            await self.nc.publish(alert_subject(alert.station), alert.model_dump_json().encode())
            ALERTS.labels(alert.rule, alert.severity, alert.state).inc()
            logger.info(
                "event=alert rule=%s state=%s station=%s sensor=%s message=%r",
                alert.rule,
                alert.state,
                alert.station,
                alert.sensor,
                alert.message,
            )

    async def _refit(self, station: str) -> None:
        detector = self.service.detector(station)
        data = detector.snapshot()
        started = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            params, loglik = await loop.run_in_executor(
                self._pool, fit_msvar, data, self.settings.regime_states, detector.params
            )
            detector.apply_fit(params)
            elapsed = time.perf_counter() - started
            FIT_SECONDS.observe(elapsed)
            logger.info(
                "event=regime_refit station=%s samples=%d loglik=%.1f seconds=%.2f",
                station,
                len(data),
                loglik,
                elapsed,
            )
        except Exception:
            logger.exception("event=regime_refit_failed station=%s", station)
        finally:
            self._fitting.discard(station)

    async def _connect(self, stop: asyncio.Event) -> bool:
        # nats-py reconnects on its own once connected; retry the *initial*
        # connect here so start order does not matter.
        backoff = 1.0
        while not stop.is_set():
            try:
                await self.nc.connect(
                    servers=[self.settings.nats_url],
                    name="forecast",
                    connect_timeout=5,
                    max_reconnect_attempts=-1,
                )
                return True
            except Exception as exc:
                logger.warning("event=nats_connect_failed backoff_s=%.0f error=%s", backoff, exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
        return False

    async def run(self, stop: asyncio.Event) -> None:
        start_http_server(self.settings.metrics_port)
        if not await self._connect(stop):
            return
        logger.info("event=nats_connected url=%s", self.settings.nats_url)
        await self.nc.subscribe("sg.stats.>", cb=self._on_stats)
        await self.nc.subscribe("sg.raw.>", cb=self._on_raw)
        logger.info("event=subscribed subjects=sg.stats.>,sg.raw.>")
        try:
            await stop.wait()
        finally:
            await self.nc.drain()
            self._pool.shutdown(cancel_futures=True)
            logger.info("event=drained")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(sig, stop.set)
    await App(settings).run(stop)
