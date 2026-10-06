"""Wire contracts consumed by the dashboard (validated against `contracts/`)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Stats(_Message):
    station: str
    sensor: int
    timestamp: int  # window start, epoch ms
    window_ms: int
    mean: float
    min: float
    max: float
    count: int
    regime: int | None = None


class ForecastMetrics(_Message):
    n: int
    mae: float | None
    coverage: float | None


class ForecastMessage(_Message):
    station: str
    sensor: int
    origin_ts: int
    step_ms: int
    timestamps: list[int]
    forecasts: list[float]
    lower_ci: list[float]
    upper_ci: list[float]
    confidence: float
    metrics: ForecastMetrics


class RegimeMessage(_Message):
    station: str
    timestamp: int
    probabilities: list[float]
    state: int
    true_regime: int | None
    mapped_state: int | None
    accuracy: float | None
    n_scored: int
