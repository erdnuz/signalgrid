"""History backfill from the archive REST API, so a page reload or frontend
restart does not start from an empty chart."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from pydantic import TypeAdapter, ValidationError

from .models import Stats
from .store import DataStore, Key

logger = logging.getLogger("frontend.archive")
_rows = TypeAdapter(list[Stats])


class ArchiveClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.session = session or requests.Session()

    def history(self, station: str, sensor: int, limit: int) -> list[Stats]:
        params: dict[str, str | int] = {"station": station, "sensor": sensor, "limit": limit}
        resp = self.session.get(
            f"{self.base_url}/stats",
            params=params,
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        return _rows.validate_python(resp.json())


class Backfiller:
    """Fetches history in the background the first time a series appears."""

    def __init__(self, client: ArchiveClient, store: DataStore) -> None:
        self.client = client
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backfill")

    def schedule(self, key: Key) -> None:
        self._pool.submit(self._run, key)

    def _run(self, key: Key) -> None:
        station, sensor = key
        try:
            rows = self.client.history(station, sensor, self.store.max_points)
        except (requests.RequestException, ValidationError) as exc:
            logger.warning("backfill_failed station=%s sensor=%s error=%s", station, sensor, exc)
            return
        added = self.store.backfill(key, rows)
        logger.info("backfilled station=%s sensor=%s rows=%d", station, sensor, added)
