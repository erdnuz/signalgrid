"""Transport-free core of the forecast service: feed it messages, get back
messages to publish. Keeping NATS out of this layer makes it unit-testable
without a broker or real time."""

from __future__ import annotations

from collections import deque

import numpy as np

from .alerts import AlertEngine
from .config import Settings
from .evaluation import ForecastEvaluator, Key, Prediction
from .models import AlertMessage, ForecastMessage, RawEvent, RegimeMessage, Stats
from .ou import CI_ALPHA, forecast_ou
from .regime import RegimeDetector, RegimeScorer


class ForecastService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.evaluator = ForecastEvaluator(
            window=settings.eval_window, horizons=(1, settings.forecast_horizon)
        )
        self.alerts = AlertEngine()
        self._series: dict[Key, deque[float]] = {}
        self._last_ts: dict[Key, int] = {}
        self._detectors: dict[str, RegimeDetector] = {}
        self._scorers: dict[str, RegimeScorer] = {}
        self._raw_seen: dict[str, int] = {}

    def detector(self, station: str) -> RegimeDetector:
        det = self._detectors.get(station)
        if det is None:
            s = self.settings
            det = self._detectors[station] = RegimeDetector(
                k=s.regime_states,
                window=s.regime_window,
                min_obs=s.regime_min_obs,
                refit_every=s.regime_refit_every,
            )
        return det

    def scorer(self, station: str) -> RegimeScorer:
        return self._scorers.setdefault(station, RegimeScorer(k=self.settings.regime_states))

    def handle_raw(self, raw: RawEvent) -> tuple[RegimeMessage | None, list[AlertMessage]]:
        """Updates the station's filtered regime probabilities. Returns a
        regime message every `regime_publish_every` updates once a model is
        fitted, plus any regime-change alerts. Refitting is the caller's job
        (it is CPU-heavy, see `app.py`)."""
        probs = self.detector(raw.station).update(raw.values)
        if probs is None:
            return None, []

        state = int(np.argmax(probs))
        scorer = self.scorer(raw.station)
        if raw.regime is not None:
            scorer.score(state, raw.regime)
        mapped = scorer.map(state) if scorer.n else state
        alerts = self.alerts.on_regime(raw.station, raw.timestamp, mapped)
        alerts += self.alerts.on_accuracy(raw.station, raw.timestamp, scorer.accuracy, scorer.n)

        seen = self._raw_seen[raw.station] = self._raw_seen.get(raw.station, 0) + 1
        if seen % self.settings.regime_publish_every:
            return None, alerts
        return RegimeMessage(
            station=raw.station,
            timestamp=raw.timestamp,
            probabilities=[round(float(p), 6) for p in probs],
            state=state,
            true_regime=raw.regime,
            mapped_state=scorer.map(state) if scorer.n else None,
            accuracy=scorer.accuracy,
            n_scored=scorer.n,
        ), alerts

    def handle_stats(self, stats: Stats) -> tuple[ForecastMessage | None, list[AlertMessage]]:
        """Scores the pending forecast for this window (raising alerts if
        needed), updates the series and, once enough history exists, returns
        a fresh forecast."""
        key: Key = (stats.station, stats.sensor)

        # Core NATS subscribers can see a window twice if forge re-publishes it
        # after a restart; only strictly newer windows advance the model.
        last = self._last_ts.get(key)
        if last is not None and stats.timestamp <= last:
            return None, []
        self._last_ts[key] = stats.timestamp

        series = self._series.setdefault(key, deque(maxlen=self.settings.max_points))
        alerts: list[AlertMessage] = []
        for score in self.evaluator.observe(key, stats.timestamp, stats.mean):
            if score.horizon == 1:  # alert rules watch the 1-step forecasts
                alerts += self.alerts.on_score(
                    key, stats.timestamp, score, self.evaluator.horizon_metrics(key, 1)
                )
        series.append(stats.mean)

        horizon = self.settings.forecast_horizon
        forecasts, intervals = forecast_ou(list(series), n_steps=horizon)
        if forecasts is None or intervals is None:
            return None, alerts

        step = stats.window_ms
        timestamps = [stats.timestamp + step * h for h in range(1, horizon + 1)]
        for h in self.evaluator.horizons:
            lo, hi = intervals[h - 1]
            self.evaluator.register(
                key, h, timestamps[h - 1], Prediction(forecasts[h - 1], lo, hi, origin_value=stats.mean)
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
        ), alerts
