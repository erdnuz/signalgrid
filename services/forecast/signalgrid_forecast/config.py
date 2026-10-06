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
    regime_states: int = 3
    # Raw samples per EM fit (5 min at 10 Hz). The first fit waits for 2 min
    # of data: fitting 3 states before all regimes have been seen splits one
    # regime in two, and simulated replays showed that 1 min -> 2 min raises
    # early accuracy from ~0.7 to ~0.9.
    regime_window: int = 3_000
    regime_min_obs: int = 1_200
    regime_refit_every: int = 600
    regime_publish_every: int = 5  # publish every Nth filtered update (2 Hz at 10 Hz input)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            nats_url=os.environ["NATS_URL"],  # required; fail fast if missing
            forecast_horizon=int(os.environ.get("FORECAST_HORIZON", cls.forecast_horizon)),
            max_points=int(os.environ.get("FORECAST_MAX_POINTS", cls.max_points)),
            eval_window=int(os.environ.get("EVAL_WINDOW", cls.eval_window)),
            metrics_port=int(os.environ.get("METRICS_PORT", cls.metrics_port)),
            regime_window=int(os.environ.get("REGIME_WINDOW", cls.regime_window)),
            regime_refit_every=int(os.environ.get("REGIME_REFIT_EVERY", cls.regime_refit_every)),
        )
