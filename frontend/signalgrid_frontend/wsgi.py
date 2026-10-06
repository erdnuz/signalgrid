"""Production entry point: `gunicorn signalgrid_frontend.wsgi:server`.

State lives in process memory and is fed by one NATS consumer thread, so the
server must run a single worker (threads are fine). Scaling out would move
the store into Redis or have the browser subscribe to NATS over WebSockets.
"""

from __future__ import annotations

import logging

from .app import create_app
from .archive import ArchiveClient, Backfiller
from .config import Settings
from .consumer import Consumer
from .store import DataStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = Settings.from_env()
store = DataStore(max_points=settings.max_points)
backfiller = Backfiller(ArchiveClient(settings.archive_url), store) if settings.archive_url else None
consumer = Consumer(settings.nats_url, store, on_new_series=backfiller.schedule if backfiller else None)
consumer.start()

app = create_app(settings, store, health=consumer.connected.is_set)
server = app.server
