"""NATS wiring for the forecast service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from pydantic import ValidationError

from .config import Settings
from .models import Stats, forecast_subject
from .service import ForecastService

logger = logging.getLogger("forecast")


class App:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = ForecastService(settings)
        self.nc = NATS()

    async def _on_stats(self, msg: Msg) -> None:
        try:
            stats = Stats.model_validate_json(msg.data)
        except ValidationError as exc:
            logger.warning("event=invalid_stats subject=%s error=%s", msg.subject, exc)
            return
        forecast = self.service.handle_stats(stats)
        if forecast is not None:
            await self.nc.publish(
                forecast_subject(forecast.station, forecast.sensor),
                forecast.model_dump_json().encode(),
            )

    async def run(self, stop: asyncio.Event) -> None:
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
                break
            except Exception as exc:
                logger.warning("event=nats_connect_failed backoff_s=%.0f error=%s", backoff, exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
        if stop.is_set():
            return
        logger.info("event=nats_connected url=%s", self.settings.nats_url)
        await self.nc.subscribe("sg.stats.>", cb=self._on_stats)
        logger.info("event=subscribed subjects=sg.stats.>")
        try:
            await stop.wait()
        finally:
            await self.nc.drain()
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
