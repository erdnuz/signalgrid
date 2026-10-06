"""NATS subscriber running on its own event loop in a daemon thread, feeding
the shared `DataStore` that Dash callbacks read from."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from pydantic import BaseModel, ValidationError

from .models import ForecastMessage, RegimeMessage, Stats
from .store import DataStore, Key

logger = logging.getLogger("frontend.consumer")


class Consumer:
    def __init__(self, nats_url: str, store: DataStore, on_new_series: Callable[[Key], None] | None = None):
        self.nats_url = nats_url
        self.store = store
        self.on_new_series = on_new_series
        self.connected = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="nats-consumer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        asyncio.run(self._main())

    def handle_stats(self, stats: Stats) -> None:
        if self.store.add_stats(stats) and self.on_new_series is not None:
            self.on_new_series((stats.station, stats.sensor))

    def _handler(
        self, model: type[BaseModel], apply: Callable[..., None]
    ) -> Callable[[Msg], Awaitable[None]]:
        async def cb(msg: Msg) -> None:
            try:
                apply(model.model_validate_json(msg.data))
            except ValidationError as exc:
                logger.warning("invalid_message subject=%s error=%s", msg.subject, exc)

        return cb

    async def _main(self) -> None:
        nc = NATS()

        async def disconnected() -> None:
            self.connected.clear()

        async def reconnected() -> None:
            self.connected.set()

        backoff = 1.0
        while True:
            try:
                await nc.connect(
                    servers=[self.nats_url],
                    name="frontend",
                    max_reconnect_attempts=-1,
                    disconnected_cb=disconnected,
                    reconnected_cb=reconnected,
                )
                break
            except Exception as exc:  # initial connect is not retried by nats-py
                logger.warning("nats_connect_failed backoff_s=%.0f error=%s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        self.connected.set()
        logger.info("nats_connected url=%s", self.nats_url)
        await nc.subscribe("sg.stats.>", cb=self._handler(Stats, self.handle_stats))
        await nc.subscribe("sg.forecasts.>", cb=self._handler(ForecastMessage, self.store.add_forecast))
        await nc.subscribe("sg.regimes.>", cb=self._handler(RegimeMessage, self.store.add_regime))
        await asyncio.Event().wait()  # run for the life of the process
