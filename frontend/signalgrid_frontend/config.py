from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    nats_url: str
    archive_url: str | None = None
    max_points: int = 120  # 60 s of 500 ms windows
    refresh_ms: int = 1000
    port: int = 8004

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            nats_url=os.environ["NATS_URL"],  # required; fail fast if missing
            archive_url=os.environ.get("ARCHIVE_URL") or None,
            max_points=int(os.environ.get("MAX_POINTS", cls.max_points)),
            refresh_ms=int(os.environ.get("REFRESH_MS", cls.refresh_ms)),
            port=int(os.environ.get("PORT", cls.port)),
        )
