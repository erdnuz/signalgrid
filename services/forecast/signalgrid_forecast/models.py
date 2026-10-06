"""Wire contracts. `RawEvent` and `Stats` mirror `signalgrid-core` (Rust);
`ForecastMessage` and `RegimeMessage` are produced here. The golden files in
`contracts/` are validated against these models in the test suite."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class RawEvent(_Message):
    station: str
    timestamp: int  # epoch ms
    values: list[float]
    regime: int | None = None


class Stats(_Message):
    station: str
    sensor: int = Field(ge=0)
    timestamp: int  # window start, epoch ms
    window_ms: int = Field(gt=0)
    mean: float
    min: float
    max: float
    count: int = Field(gt=0)
    regime: int | None = None


class HorizonMetrics(_Message):
    """Rolling out-of-sample quality of h-step-ahead forecasts."""

    horizon: int  # windows ahead
    n: int
    mae: float | None
    mase: float | None  # model MAE / persistence MAE at the same horizon; < 1 beats it
    coverage: float | None  # share of actuals inside the interval


class ForecastMessage(_Message):
    station: str
    sensor: int
    origin_ts: int  # window the forecast was made from
    step_ms: int
    timestamps: list[int]
    forecasts: list[float]
    lower_ci: list[float]
    upper_ci: list[float]
    confidence: float
    metrics: list[HorizonMetrics]  # one entry per evaluated horizon, ascending

    @field_validator("forecasts", "lower_ci", "upper_ci")
    @classmethod
    def _non_empty(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("must not be empty")
        return v


class RegimeMessage(_Message):
    station: str
    timestamp: int
    probabilities: list[float]  # filtered P(state | data so far), canonical order
    state: int  # argmax, canonical order (by volatility)
    true_regime: int | None
    mapped_state: int | None  # state relabelled to the best-matching true regime
    accuracy: float | None  # rolling accuracy of mapped_state vs true_regime
    n_scored: int


Severity = Literal["info", "warning", "serious", "critical"]


class AlertMessage(_Message):
    """A state change of an alert rule. `firing` alerts stay active until a
    matching `resolved` message (same `rule` and scope) arrives; one-shot
    events (anomalies, regime changes) are sent as `firing` only."""

    id: str
    rule: Literal["anomaly", "calibration", "model_degraded", "regime_change", "regime_accuracy"]
    severity: Severity
    state: Literal["firing", "resolved"]
    station: str
    sensor: int | None
    timestamp: int
    message: str
    value: float | None = None


def subject_token(value: str | int) -> str:
    token = str(value)
    if not token or any(c in token for c in ". *>"):
        raise ValueError(f"invalid subject token {token!r}")
    return token


def forecast_subject(station: str, sensor: int) -> str:
    return f"sg.forecasts.{subject_token(station)}.{subject_token(sensor)}"


def regime_subject(station: str) -> str:
    return f"sg.regimes.{subject_token(station)}"


def alert_subject(station: str) -> str:
    return f"sg.alerts.{subject_token(station)}"
