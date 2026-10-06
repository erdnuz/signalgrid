"""Online, out-of-sample evaluation of 1-step-ahead forecasts.

Each forecast registers a prediction for the next window. When the actual
stats for that window arrive, the absolute error and whether the actual fell
inside the prediction interval are recorded in a rolling window. Empirical
coverage close to the nominal level is the check that the intervals are
calibrated, not just plausible-looking.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .models import ForecastMetrics

Key = tuple[str, int]


@dataclass(frozen=True)
class Prediction:
    point: float
    lower: float
    upper: float


@dataclass(frozen=True)
class Score:
    abs_error: float
    covered: bool


@dataclass
class ForecastEvaluator:
    window: int = 200
    _pending: dict[Key, dict[int, Prediction]] = field(default_factory=lambda: defaultdict(dict))
    _scores: dict[Key, deque[Score]] = field(default_factory=dict)

    def register(self, key: Key, target_ts: int, prediction: Prediction) -> None:
        self._pending[key][target_ts] = prediction

    def observe(self, key: Key, ts: int, actual: float) -> Score | None:
        pending = self._pending.get(key)
        if not pending:
            return None
        prediction = pending.pop(ts, None)
        # Predictions for windows that were skipped can never be scored.
        for stale in [t for t in pending if t < ts]:
            del pending[stale]
        if prediction is None:
            return None

        score = Score(
            abs_error=abs(actual - prediction.point),
            covered=prediction.lower <= actual <= prediction.upper,
        )
        self._scores.setdefault(key, deque(maxlen=self.window)).append(score)
        return score

    def metrics(self, key: Key) -> ForecastMetrics:
        return _summarise(list(self._scores.get(key, ())))

    def overall(self) -> ForecastMetrics:
        return _summarise([s for scores in self._scores.values() for s in scores])


def _summarise(scores: list[Score]) -> ForecastMetrics:
    if not scores:
        return ForecastMetrics(n=0, mae=None, coverage=None)
    n = len(scores)
    return ForecastMetrics(
        n=n,
        mae=sum(s.abs_error for s in scores) / n,
        coverage=sum(s.covered for s in scores) / n,
    )
