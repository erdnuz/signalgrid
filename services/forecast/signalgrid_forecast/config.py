from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    nats_url: str
    forecast_horizon: int = 3
    max_points: int = 60
    eval_window: int = 200
    metrics_port: int = 9000

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            nats_url=os.environ["NATS_URL"],  # required; fail fast if missing
            forecast_horizon=int(os.environ.get("FORECAST_HORIZON", cls.forecast_horizon)),
            max_points=int(os.environ.get("FORECAST_MAX_POINTS", cls.max_points)),
            eval_window=int(os.environ.get("EVAL_WINDOW", cls.eval_window)),
            metrics_port=int(os.environ.get("METRICS_PORT", cls.metrics_port)),
        )
