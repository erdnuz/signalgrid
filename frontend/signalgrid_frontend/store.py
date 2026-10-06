"""Thread-safe in-memory view of the live pipeline.

The NATS consumer thread writes; Dash callback threads read immutable
snapshots. All buffers are bounded deques, so memory does not grow with
uptime.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import ForecastMessage, ForecastMetrics, RegimeMessage, Stats

Key = tuple[str, int]

LATENCY_SAMPLES = 500
THROUGHPUT_WINDOW_S = 10.0


@dataclass(frozen=True)
class SeriesView:
    station: str
    sensor: int
    timestamps: list[int]
    actuals: list[float]
    # Rolling 1-step-ahead forecasts followed by the latest multi-step horizon.
    forecast_ts: list[int]
    forecasts: list[float]
    lower_ci: list[float]
    upper_ci: list[float]
    confidence: float | None
    metrics: ForecastMetrics | None


@dataclass(frozen=True)
class RegimeView:
    timestamps: list[int]
    true_regime: list[int | None]
    detected: list[int | None]
    accuracy: float | None
    n_scored: int


@dataclass(frozen=True)
class Kpis:
    mae: float | None
    coverage: float | None
    confidence: float | None
    regime_accuracy: float | None
    latency_ms_p50: float | None
    windows_per_s: float


@dataclass
class _Series:
    maxlen: int
    timestamps: deque[int] = field(init=False)
    actuals: deque[float] = field(init=False)
    one_step: deque[tuple[int, float, float, float]] = field(init=False)
    horizon: ForecastMessage | None = None

    def __post_init__(self) -> None:
        self.timestamps = deque(maxlen=self.maxlen)
        self.actuals = deque(maxlen=self.maxlen)
        self.one_step = deque(maxlen=self.maxlen)


class DataStore:
    def __init__(self, max_points: int = 120) -> None:
        self.max_points = max_points
        self._lock = threading.Lock()
        self._series: dict[Key, _Series] = {}
        self._regimes: dict[str, deque[RegimeMessage]] = {}
        self._latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self._arrivals: deque[float] = deque()

    # ---- writers -------------------------------------------------------

    def add_stats(self, stats: Stats, received_ms: int | None = None) -> bool:
        """Appends a window; returns True the first time a key is seen."""
        now = time.time()
        received = received_ms if received_ms is not None else int(now * 1000)
        key = (stats.station, stats.sensor)
        with self._lock:
            series = self._series.get(key)
            is_new = series is None
            if series is None:
                series = self._series[key] = _Series(self.max_points)
            if series.timestamps and stats.timestamp <= series.timestamps[-1]:
                return is_new  # duplicate / out-of-order window
            series.timestamps.append(stats.timestamp)
            series.actuals.append(stats.mean)

            self._latency_ms.append(received - (stats.timestamp + stats.window_ms))
            self._arrivals.append(now)
            while self._arrivals and now - self._arrivals[0] > THROUGHPUT_WINDOW_S:
                self._arrivals.popleft()
        return is_new

    def backfill(self, key: Key, history: Iterable[Stats]) -> int:
        """Prepends archived windows older than anything live. Returns rows added."""
        with self._lock:
            series = self._series.setdefault(key, _Series(self.max_points))
            first_live = series.timestamps[0] if series.timestamps else None
            older = sorted(
                (s for s in history if first_live is None or s.timestamp < first_live),
                key=lambda s: s.timestamp,
            )
            room = self.max_points - len(series.timestamps)
            older = older[-room:] if room > 0 else []
            for s in reversed(older):
                series.timestamps.appendleft(s.timestamp)
                series.actuals.appendleft(s.mean)
            return len(older)

    def add_forecast(self, forecast: ForecastMessage) -> None:
        key = (forecast.station, forecast.sensor)
        with self._lock:
            series = self._series.setdefault(key, _Series(self.max_points))
            if series.horizon is not None and forecast.origin_ts <= series.horizon.origin_ts:
                return
            series.horizon = forecast
            series.one_step.append(
                (
                    forecast.timestamps[0],
                    forecast.forecasts[0],
                    forecast.lower_ci[0],
                    forecast.upper_ci[0],
                )
            )

    def add_regime(self, regime: RegimeMessage) -> None:
        with self._lock:
            buf = self._regimes.setdefault(regime.station, deque(maxlen=self.max_points * 5))
            buf.append(regime)

    # ---- readers -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "series": len(self._series),
                "forecasts": sum(1 for s in self._series.values() if s.horizon is not None),
                "regime_stations": len(self._regimes),
            }

    def stations(self) -> list[str]:
        with self._lock:
            return sorted({station for station, _ in self._series})

    def sensors(self, station: str | None) -> list[int]:
        with self._lock:
            return sorted({sensor for st, sensor in self._series if st == station})

    def series(self, station: str, sensor: int) -> SeriesView | None:
        with self._lock:
            s = self._series.get((station, sensor))
            if s is None:
                return None
            one_step = list(s.one_step)
            horizon = s.horizon
            if horizon is not None:
                # The horizon's first step is already the newest 1-step entry.
                tail = list(
                    zip(
                        horizon.timestamps[1:],
                        horizon.forecasts[1:],
                        horizon.lower_ci[1:],
                        horizon.upper_ci[1:],
                        strict=True,
                    )
                )
                one_step += tail
            return SeriesView(
                station=station,
                sensor=sensor,
                timestamps=list(s.timestamps),
                actuals=list(s.actuals),
                forecast_ts=[p[0] for p in one_step],
                forecasts=[p[1] for p in one_step],
                lower_ci=[p[2] for p in one_step],
                upper_ci=[p[3] for p in one_step],
                confidence=horizon.confidence if horizon else None,
                metrics=horizon.metrics if horizon else None,
            )

    def regimes(self, station: str) -> RegimeView | None:
        with self._lock:
            buf = list(self._regimes.get(station, ()))
        if not buf:
            return None
        return RegimeView(
            timestamps=[r.timestamp for r in buf],
            true_regime=[r.true_regime for r in buf],
            detected=[r.mapped_state for r in buf],
            accuracy=buf[-1].accuracy,
            n_scored=buf[-1].n_scored,
        )

    def kpis(self, station: str | None, sensor: int | None) -> Kpis:
        view = self.series(station, sensor) if station is not None and sensor is not None else None
        regime = self.regimes(station) if station is not None else None
        with self._lock:
            latency = statistics.median(self._latency_ms) if self._latency_ms else None
            rate = len(self._arrivals) / THROUGHPUT_WINDOW_S
        metrics = view.metrics if view else None
        return Kpis(
            mae=metrics.mae if metrics else None,
            coverage=metrics.coverage if metrics else None,
            confidence=view.confidence if view else None,
            regime_accuracy=regime.accuracy if regime else None,
            latency_ms_p50=latency,
            windows_per_s=rate,
        )
