"""Online, out-of-sample evaluation of forecasts at several horizons.

Each forecast registers predictions for chosen horizons (1 and 3 windows
ahead by default). When the actual stats for a target window arrive, the
error is recorded in a rolling window per (series, horizon), together with
the error of a naive persistence forecast at the same horizon ("the value h
windows ahead equals the value at forecast time"). Two summaries come out:

* **MASE** (mean absolute scaled error, Hyndman & Koehler 2006): model MAE /
  naive MAE over the same windows. Scale-free, so it compares across
  sensors, stations and horizons, and well defined for zero-centred signals
  (unlike MAPE, which explodes as actuals approach 0). Below 1 means the
  model beats persistence at that horizon.
* **Coverage**: share of actuals inside the prediction interval. Close to the
  nominal level means the intervals are calibrated, not just plausible.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import HorizonMetrics

Key = tuple[str, int]


@dataclass(frozen=True)
class Prediction:
    point: float
    lower: float
    upper: float
    #: last actual when the forecast was made (the persistence forecast)
    origin_value: float | None = None


@dataclass(frozen=True)
class Score:
    horizon: int
    prediction: Prediction
    actual: float
    abs_error: float
    naive_abs_error: float | None
    covered: bool


@dataclass
class ForecastEvaluator:
    window: int = 200
    horizons: tuple[int, ...] = (1, 3)
    _pending: dict[tuple[Key, int], dict[int, Prediction]] = field(default_factory=dict)
    _scores: dict[tuple[Key, int], deque[Score]] = field(default_factory=dict)

    def register(self, key: Key, horizon: int, target_ts: int, prediction: Prediction) -> None:
        self._pending.setdefault((key, horizon), {})[target_ts] = prediction

    def observe(self, key: Key, ts: int, actual: float) -> list[Score]:
        """Scores every pending prediction (any horizon) targeting window `ts`."""
        scores = []
        for h in self.horizons:
            pending = self._pending.get((key, h))
            if not pending:
                continue
            prediction = pending.pop(ts, None)
            # Predictions for windows that were skipped can never be scored.
            for stale in [t for t in pending if t < ts]:
                del pending[stale]
            if prediction is None:
                continue
            origin = prediction.origin_value
            score = Score(
                horizon=h,
                prediction=prediction,
                actual=actual,
                abs_error=abs(actual - prediction.point),
                naive_abs_error=None if origin is None else abs(actual - origin),
                covered=prediction.lower <= actual <= prediction.upper,
            )
            self._scores.setdefault((key, h), deque(maxlen=self.window)).append(score)
            scores.append(score)
        return scores

    def metrics(self, key: Key) -> list[HorizonMetrics]:
        return [_summarise(h, list(self._scores.get((key, h), ()))) for h in self.horizons]

    def horizon_metrics(self, key: Key, horizon: int) -> HorizonMetrics:
        return _summarise(horizon, list(self._scores.get((key, horizon), ())))


def _summarise(horizon: int, scores: list[Score]) -> HorizonMetrics:
    if not scores:
        return HorizonMetrics(horizon=horizon, n=0, mae=None, mase=None, coverage=None)
    n = len(scores)
    paired = [s for s in scores if s.naive_abs_error is not None]
    naive = sum(s.naive_abs_error or 0.0 for s in paired)
    model = sum(s.abs_error for s in paired)
    return HorizonMetrics(
        horizon=horizon,
        n=n,
        mae=sum(s.abs_error for s in scores) / n,
        mase=model / naive if naive > 0 else None,
        coverage=sum(s.covered for s in scores) / n,
    )
