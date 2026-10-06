"""Thread-safe in-memory view of the live pipeline.

The NATS consumer thread writes; Dash callback threads read immutable
snapshots. Every buffer is a bounded deque sized for the longest selectable
time range, so memory does not grow with uptime.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import AlertMessage, ForecastMessage, HorizonMetrics, RegimeMessage, Stats

Key = tuple[str, int]

LATENCY_SAMPLES = 500
THROUGHPUT_WINDOW_S = 10.0
ALERT_LOG = 200
#: one-shot alerts (anomaly, regime change) count as active for this long
EVENT_ALERT_TTL_MS = 30_000
#: a series with no new window for this many window lengths is stale
STALE_WINDOWS = 6
#: latency alert: per-second median above this for LATENCY_SUSTAIN_S seconds
LATENCY_ALERT_MS = 100.0
LATENCY_SUSTAIN_S = 30
#: rolling metrics from fewer scored windows than this are too noisy to plot
MIN_SCORED = 20
#: forecast horizons (windows ahead) the forecast service evaluates
HORIZONS = (1, 3)


@dataclass(frozen=True)
class SeriesView:
    station: str
    sensor: int
    #: bumped when history is backfilled, so clients know to redraw
    epoch: int
    window_ms: int
    timestamps: list[int]
    actuals: list[float]
    # Rolling 1-step-ahead forecasts (one per window, append-only).
    one_step_ts: list[int]
    one_step: list[float]
    one_step_lo: list[float]
    one_step_hi: list[float]
    # Latest multi-step horizon (replaced by every new forecast).
    horizon_ts: list[int]
    horizon: list[float]
    horizon_lo: list[float]
    horizon_hi: list[float]
    confidence: float | None
    #: latest rolling quality per horizon (windows ahead)
    metrics: dict[int, HorizonMetrics]
    # Rolling forecast quality over time, per horizon.
    metric_ts: list[int]
    mase: dict[int, list[float | None]]
    coverage: dict[int, list[float | None]]


@dataclass(frozen=True)
class RegimeView:
    timestamps: list[int]
    true_regime: list[int | None]
    detected: list[int | None]
    accuracy: list[float | None]
    n_scored: int


@dataclass(frozen=True)
class Kpis:
    """Headline numbers for the current selection (means over its series)."""

    mase: dict[int, float | None]  # per horizon
    coverage: dict[int, float | None]
    confidence: float | None
    regime_accuracy: float | None
    active_alerts: int
    severe_alerts: int  # serious or critical
    latency_ms_p50: float | None
    windows_per_s: float


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


@dataclass
class _Series:
    capacity: int
    epoch: int = 0
    window_ms: int = 500
    timestamps: deque[int] = field(init=False)
    actuals: deque[float] = field(init=False)
    one_step: deque[tuple[int, float, float, float]] = field(init=False)
    metrics: deque[tuple[int, dict[int, tuple[float | None, float | None]]]] = field(init=False)
    horizon: ForecastMessage | None = None
    last_received_ms: int = 0

    def __post_init__(self) -> None:
        self.timestamps = deque(maxlen=self.capacity)
        self.actuals = deque(maxlen=self.capacity)
        self.one_step = deque(maxlen=self.capacity)
        self.metrics = deque(maxlen=self.capacity)


class DataStore:
    def __init__(self, max_points: int = 1_800) -> None:
        #: points kept per series (1 800 = 15 min of 500 ms windows)
        self.max_points = max_points
        self._lock = threading.Lock()
        self._series: dict[Key, _Series] = {}
        self._regimes: dict[str, deque[RegimeMessage]] = {}
        self._latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self._arrivals: deque[float] = deque()
        # Per-second median latency, for the latency-over-time chart.
        self._latency_history: deque[tuple[int, float]] = deque(maxlen=max_points)
        self._latency_bucket: tuple[int, list[float]] | None = None
        self._alerts: deque[AlertMessage] = deque(maxlen=ALERT_LOG)
        #: state-based alerts currently firing, keyed by (rule, station, sensor)
        self._firing: dict[tuple[str, str, int | None], AlertMessage] = {}

    def _series_for(self, key: Key) -> _Series:
        series = self._series.get(key)
        if series is None:
            series = self._series[key] = _Series(self.max_points)
        return series

    # ---- writers -------------------------------------------------------

    def add_stats(self, stats: Stats, received_ms: int | None = None) -> bool:
        """Appends a window; returns True the first time a key is seen."""
        now = time.time()
        received = received_ms if received_ms is not None else int(now * 1000)
        key = (stats.station, stats.sensor)
        with self._lock:
            is_new = key not in self._series
            series = self._series_for(key)
            if series.timestamps and stats.timestamp <= series.timestamps[-1]:
                return is_new  # duplicate / out-of-order window
            series.window_ms = stats.window_ms
            series.last_received_ms = received
            series.timestamps.append(stats.timestamp)
            series.actuals.append(stats.mean)

            latency = float(received - (stats.timestamp + stats.window_ms))
            self._latency_ms.append(latency)
            self._record_latency(received // 1000 * 1000, latency)
            self._arrivals.append(now)
            while self._arrivals and now - self._arrivals[0] > THROUGHPUT_WINDOW_S:
                self._arrivals.popleft()
        return is_new

    def _record_latency(self, second_ms: int, latency: float) -> None:
        bucket = self._latency_bucket
        if bucket is not None and bucket[0] != second_ms:
            self._latency_history.append((bucket[0], statistics.median(bucket[1])))
            bucket = None
        if bucket is None:
            bucket = self._latency_bucket = (second_ms, [])
        bucket[1].append(latency)

    def backfill(self, key: Key, history: Iterable[Stats]) -> int:
        """Prepends archived windows older than anything live. Returns rows added."""
        with self._lock:
            series = self._series_for(key)
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
            if older:
                series.window_ms = older[-1].window_ms
                series.epoch += 1
            return len(older)

    def add_forecast(self, forecast: ForecastMessage) -> None:
        key = (forecast.station, forecast.sensor)
        with self._lock:
            series = self._series_for(key)
            if series.horizon is not None and forecast.origin_ts <= series.horizon.origin_ts:
                return
            series.horizon = forecast
            series.one_step.append(
                (forecast.timestamps[0], forecast.forecasts[0], forecast.lower_ci[0], forecast.upper_ci[0])
            )
            scored = {m.horizon: (m.mase, m.coverage) for m in forecast.metrics if m.n >= MIN_SCORED}
            if scored:
                series.metrics.append((forecast.origin_ts, scored))

    def add_regime(self, regime: RegimeMessage) -> None:
        with self._lock:
            buf = self._regimes.get(regime.station)
            if buf is None:
                buf = self._regimes[regime.station] = deque(maxlen=self.max_points)
            if buf and regime.timestamp <= buf[-1].timestamp:
                return
            buf.append(regime)

    def add_alert(self, alert: AlertMessage) -> None:
        with self._lock:
            self._alerts.append(alert)
            key = (alert.rule, alert.station, alert.sensor)
            if alert.state == "resolved":
                self._firing.pop(key, None)
            elif alert.rule in ("calibration", "model_degraded", "regime_accuracy"):
                self._firing[key] = alert

    # ---- readers -------------------------------------------------------

    def keys(self) -> list[Key]:
        with self._lock:
            return sorted(self._series)

    def alerts(self, now_ms: int | None = None) -> tuple[list[AlertMessage], list[AlertMessage]]:
        """(recent log newest first, currently active). Active = firing
        state-based alerts, recent one-shot events, and derived stale-data
        alerts for series that stopped updating."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._lock:
            log = list(reversed(self._alerts))
            active = list(self._firing.values())
            active += [
                a
                for a in self._alerts
                if a.rule in ("anomaly", "regime_change") and now - a.timestamp <= EVENT_ALERT_TTL_MS
            ]
            stale = [
                AlertMessage(
                    id=f"stale:{station}:{sensor}",
                    rule="stale",
                    severity="critical",
                    state="firing",
                    station=station,
                    sensor=sensor,
                    timestamp=s.last_received_ms,
                    message=f"No new window for {(now - s.last_received_ms) / 1000:.0f} s",
                )
                for (station, sensor), s in self._series.items()
                if s.last_received_ms and now - s.last_received_ms > STALE_WINDOWS * s.window_ms
            ]
            recent = list(self._latency_history)[-LATENCY_SUSTAIN_S:]
        derived = stale
        if len(recent) == LATENCY_SUSTAIN_S and all(v > LATENCY_ALERT_MS for _, v in recent):
            worst = max(v for _, v in recent)
            derived = [
                *stale,
                *[
                    AlertMessage(
                        id=f"latency:{station}",
                        rule="latency",
                        severity="warning",
                        state="firing",
                        station=station,
                        sensor=None,
                        timestamp=recent[-1][0],
                        message=(
                            f"Pipeline latency above {LATENCY_ALERT_MS:.0f} ms for "
                            f"{LATENCY_SUSTAIN_S} s (peak {worst:.0f} ms)"
                        ),
                    )
                    for station in sorted({k[0] for k in self._series})
                ],
            ]
        return derived + log, derived + active

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
            metrics = list(s.metrics)
            h = s.horizon
            return SeriesView(
                station=station,
                sensor=sensor,
                epoch=s.epoch,
                window_ms=s.window_ms,
                timestamps=list(s.timestamps),
                actuals=list(s.actuals),
                one_step_ts=[p[0] for p in one_step],
                one_step=[p[1] for p in one_step],
                one_step_lo=[p[2] for p in one_step],
                one_step_hi=[p[3] for p in one_step],
                horizon_ts=list(h.timestamps) if h else [],
                horizon=list(h.forecasts) if h else [],
                horizon_lo=list(h.lower_ci) if h else [],
                horizon_hi=list(h.upper_ci) if h else [],
                confidence=h.confidence if h else None,
                metrics={m.horizon: m for m in h.metrics} if h else {},
                metric_ts=[m[0] for m in metrics],
                mase={hz: [m[1].get(hz, (None, None))[0] for m in metrics] for hz in HORIZONS},
                coverage={hz: [m[1].get(hz, (None, None))[1] for m in metrics] for hz in HORIZONS},
            )

    def regimes(self, station: str, since: int | None = None) -> RegimeView | None:
        with self._lock:
            buf = list(self._regimes.get(station, ()))
        if since is not None:
            buf = [r for r in buf if r.timestamp >= since]
        if not buf:
            return None
        return RegimeView(
            timestamps=[r.timestamp for r in buf],
            true_regime=[r.true_regime for r in buf],
            detected=[r.mapped_state for r in buf],
            accuracy=[r.accuracy for r in buf],
            n_scored=buf[-1].n_scored,
        )

    def latency_history(self) -> tuple[list[int], list[float]]:
        with self._lock:
            points = list(self._latency_history)
        return [p[0] for p in points], [p[1] for p in points]

    def kpis(self, keys: Iterable[Key], stations: Iterable[str]) -> Kpis:
        keys, stations = list(keys), set(stations)
        views = [v for k in keys if (v := self.series(*k)) is not None]
        regimes = [r for st in stations if (r := self.regimes(st)) is not None]
        _, active = self.alerts()
        scoped = [
            a for a in active if a.station in stations and (a.sensor is None or (a.station, a.sensor) in keys)
        ]
        with self._lock:
            latency = statistics.median(self._latency_ms) if self._latency_ms else None
            rate = len(self._arrivals) / THROUGHPUT_WINDOW_S
        return Kpis(
            mase={hz: _mean(v.metrics[hz].mase for v in views if hz in v.metrics) for hz in HORIZONS},
            coverage={hz: _mean(v.metrics[hz].coverage for v in views if hz in v.metrics) for hz in HORIZONS},
            confidence=next((v.confidence for v in views if v.confidence), None),
            regime_accuracy=_mean(r.accuracy[-1] for r in regimes),
            active_alerts=len(scoped),
            severe_alerts=sum(a.severity in ("serious", "critical") for a in scoped),
            latency_ms_p50=latency,
            windows_per_s=rate,
        )
