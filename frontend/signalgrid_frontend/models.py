"""Wire contracts consumed by the dashboard (validated against `contracts/`)."""

from __future__ import annotations

from typing import Literal

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


class HorizonMetrics(_Message):
    horizon: int
    n: int
    mae: float | None
    mase: float | None
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
    metrics: list[HorizonMetrics]


class RegimeMessage(_Message):
    station: str
    timestamp: int
    probabilities: list[float]
    state: int
    true_regime: int | None
    mapped_state: int | None
    accuracy: float | None
    n_scored: int


Severity = Literal["info", "warning", "serious", "critical"]


class AlertMessage(_Message):
    id: str
    rule: str
    severity: Severity
    state: Literal["firing", "resolved"]
    station: str
    sensor: int | None
    timestamp: int
    message: str
    value: float | None = None
