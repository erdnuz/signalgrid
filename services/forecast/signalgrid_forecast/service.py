"""Transport-free core of the forecast service: feed it messages, get back
messages to publish. Keeping NATS out of this layer makes it unit-testable
without a broker or real time."""

from __future__ import annotations

from collections import deque

from .config import Settings
from .evaluation import ForecastEvaluator, Key, Prediction
from .models import ForecastMessage, Stats
from .ou import CI_ALPHA, forecast_ou


class ForecastService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.evaluator = ForecastEvaluator(window=settings.eval_window)
        self._series: dict[Key, deque[float]] = {}
        self._last_ts: dict[Key, int] = {}

    def handle_stats(self, stats: Stats) -> ForecastMessage | None:
        """Scores the pending forecast for this window, updates the series and,
        once enough history exists, returns a fresh forecast."""
        key: Key = (stats.station, stats.sensor)

        # Core NATS subscribers can see a window twice if forge re-publishes it
        # after a restart; only strictly newer windows advance the model.
        last = self._last_ts.get(key)
        if last is not None and stats.timestamp <= last:
            return None
        self._last_ts[key] = stats.timestamp

        self.evaluator.observe(key, stats.timestamp, stats.mean)
        series = self._series.setdefault(key, deque(maxlen=self.settings.max_points))
        series.append(stats.mean)

        horizon = self.settings.forecast_horizon
        forecasts, intervals = forecast_ou(list(series), n_steps=horizon)
        if forecasts is None or intervals is None:
            return None

        step = stats.window_ms
        timestamps = [stats.timestamp + step * h for h in range(1, horizon + 1)]
        self.evaluator.register(
            key,
            timestamps[0],
            Prediction(point=forecasts[0], lower=intervals[0][0], upper=intervals[0][1]),
        )

        return ForecastMessage(
            station=stats.station,
            sensor=stats.sensor,
            origin_ts=stats.timestamp,
            step_ms=step,
            timestamps=timestamps,
            forecasts=forecasts,
            lower_ci=[lo for lo, _ in intervals],
            upper_ci=[hi for _, hi in intervals],
            confidence=1 - CI_ALPHA,
            metrics=self.evaluator.metrics(key),
        )
