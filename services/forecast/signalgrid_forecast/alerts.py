"""Alert rules evaluated next to the models that produce the signals.

* **anomaly**: window means fall outside their 1-step prediction interval
  twice in a row (warning), or once by more than a half-width (serious).
  Rate-limited per series by a cooldown.
* **calibration**: rolling interval coverage drops below a floor (serious).
* **model_degraded**: rolling MASE exceeds a ceiling, i.e. the model is
  doing worse than a naive persistence forecast (warning).
* **regime_change**: the detected regime switches and holds for a while (info).
* **regime_accuracy**: detection accuracy below a floor for 30 s (warning).

State-based rules (calibration, model_degraded) use hysteresis: they fire
below/above a threshold and only resolve once the metric has recovered past a
margin, so a metric hovering at the threshold does not flap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evaluation import Key, Score
from .models import AlertMessage, HorizonMetrics, Severity


@dataclass
class AlertEngine:
    coverage_floor: float = 0.93
    coverage_recover: float = 0.95
    mase_ceiling: float = 1.10
    mase_recover: float = 1.02
    min_scored: int = 100
    anomaly_cooldown_ms: int = 10_000
    consecutive_misses: int = 2
    regime_persist: int = 20  # consecutive filtered updates (2 s at 10 Hz)
    accuracy_floor: float = 0.70
    accuracy_recover: float = 0.75
    #: the condition must hold this long before firing (no alerts on blips)
    sustain_ms: int = 30_000
    _since: dict[tuple[str, Key], int] = field(default_factory=dict)

    _firing: set[tuple[str, Key]] = field(default_factory=set)
    _last_anomaly: dict[Key, int] = field(default_factory=dict)
    _misses: dict[Key, int] = field(default_factory=dict)
    _regime: dict[str, int] = field(default_factory=dict)
    _candidate: dict[str, tuple[int, int]] = field(default_factory=dict)

    def on_score(self, key: Key, ts: int, score: Score, metrics: HorizonMetrics) -> list[AlertMessage]:
        station, sensor = key
        out: list[AlertMessage] = []

        # A 98% interval *should* miss ~2% of windows, so one miss is not news.
        # Alert on consecutive misses (~0.04% by chance) or one far outside.
        misses = self._misses[key] = 0 if score.covered else self._misses.get(key, 0) + 1
        p = score.prediction
        half = (p.upper - p.lower) / 2
        excess = (score.actual - p.upper) if score.actual > p.upper else (p.lower - score.actual)
        extreme = misses > 0 and half > 0 and excess > half
        cooled = ts - self._last_anomaly.get(key, -(10**15)) >= self.anomaly_cooldown_ms
        if (extreme or misses >= self.consecutive_misses) and cooled:
            severity: Severity = "serious" if extreme else "warning"
            self._last_anomaly[key] = ts
            out.append(
                AlertMessage(
                    id=f"anomaly:{station}:{sensor}:{ts}",
                    rule="anomaly",
                    severity=severity,
                    state="firing",
                    station=station,
                    sensor=sensor,
                    timestamp=ts,
                    message=(
                        f"Window mean {score.actual:.2f} outside the forecast interval "
                        f"[{p.lower:.2f}, {p.upper:.2f}]"
                    ),
                    value=score.actual,
                )
            )

        if metrics.n >= self.min_scored and metrics.coverage is not None:
            out += self._hysteresis(
                "calibration",
                key,
                ts,
                value=metrics.coverage,
                fire=metrics.coverage < self.coverage_floor,
                recover=metrics.coverage >= self.coverage_recover,
                severity="serious",
                fire_msg=f"Interval coverage {metrics.coverage:.1%} below {self.coverage_floor:.0%}",
                ok_msg=f"Interval coverage recovered to {metrics.coverage:.1%}",
            )
        if metrics.n >= self.min_scored and metrics.mase is not None:
            out += self._hysteresis(
                "model_degraded",
                key,
                ts,
                value=metrics.mase,
                fire=metrics.mase > self.mase_ceiling,
                recover=metrics.mase <= self.mase_recover,
                severity="warning",
                fire_msg=f"MASE {metrics.mase:.2f}: forecasts worse than naive persistence",
                ok_msg=f"MASE recovered to {metrics.mase:.2f}",
            )
        return out

    def _hysteresis(
        self,
        rule: str,
        key: Key,
        ts: int,
        *,
        value: float,
        fire: bool,
        recover: bool,
        severity: Severity,
        fire_msg: str,
        ok_msg: str,
    ) -> list[AlertMessage]:
        state_key = (rule, key)
        firing = state_key in self._firing
        if not firing and fire:
            self._firing.add(state_key)
            state, msg = "firing", fire_msg
        elif firing and recover:
            self._firing.discard(state_key)
            state, msg = "resolved", ok_msg
        else:
            return []
        station, sensor = key
        return [
            AlertMessage.model_validate(
                {
                    "id": f"{rule}:{station}:{sensor}:{ts}",
                    "rule": rule,
                    "severity": severity if state == "firing" else "info",
                    "state": state,
                    "station": station,
                    "sensor": sensor,
                    "timestamp": ts,
                    "message": msg,
                    "value": value,
                }
            )
        ]

    def on_accuracy(self, station: str, ts: int, accuracy: float | None, n_scored: int) -> list[AlertMessage]:
        """Regime-detection accuracy below the floor for `sustain_ms`."""
        if accuracy is None or n_scored < 600:
            return []
        key: Key = (station, -1)
        low = accuracy < self.accuracy_floor
        if low:
            self._since.setdefault(("regime_accuracy", key), ts)
        else:
            self._since.pop(("regime_accuracy", key), None)
        sustained = low and ts - self._since.get(("regime_accuracy", key), ts) >= self.sustain_ms
        out = self._hysteresis(
            "regime_accuracy",
            key,
            ts,
            value=accuracy,
            fire=sustained,
            recover=accuracy >= self.accuracy_recover,
            severity="warning",
            fire_msg=f"Regime detection accuracy {accuracy:.0%} below {self.accuracy_floor:.0%} for 30 s",
            ok_msg=f"Regime detection accuracy recovered to {accuracy:.0%}",
        )
        return [a.model_copy(update={"sensor": None}) for a in out]

    def on_regime(self, station: str, ts: int, state: int | None) -> list[AlertMessage]:
        """Fires once a newly detected regime has held for `regime_persist` updates."""
        if state is None:
            return []
        current = self._regime.get(station)
        if state == current:
            self._candidate.pop(station, None)
            return []
        cand_state, count = self._candidate.get(station, (state, 0))
        count = count + 1 if cand_state == state else 1
        self._candidate[station] = (state, count)
        if count < self.regime_persist:
            return []
        self._regime[station] = state
        self._candidate.pop(station, None)
        if current is None:
            return []  # first confirmed regime: nothing changed
        return [
            AlertMessage(
                id=f"regime_change:{station}:{ts}",
                rule="regime_change",
                severity="info",
                state="firing",
                station=station,
                sensor=None,
                timestamp=ts,
                message=f"Regime change detected: R{current} → R{state}",
                value=float(state),
            )
        ]
