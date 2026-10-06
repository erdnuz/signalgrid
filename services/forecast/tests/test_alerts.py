from signalgrid_forecast.alerts import AlertEngine
from signalgrid_forecast.evaluation import Prediction, Score
from signalgrid_forecast.models import HorizonMetrics

KEY = ("StationA", 1)
OK = HorizonMetrics(horizon=1, n=150, mae=0.1, mase=0.95, coverage=0.98)


def score(actual: float, lo: float = -1.0, hi: float = 1.0) -> Score:
    p = Prediction(point=(lo + hi) / 2, lower=lo, upper=hi)
    return Score(1, p, actual, abs(actual - p.point), 0.1, lo <= actual <= hi)


def test_single_miss_is_expected_but_consecutive_or_extreme_misses_alert():
    eng = AlertEngine(anomaly_cooldown_ms=1_000)
    assert eng.on_score(KEY, 0, score(1.5), OK) == []  # one ordinary miss: expected 2% of the time
    (warn,) = eng.on_score(KEY, 500, score(1.5), OK)  # second in a row
    assert (warn.rule, warn.severity) == ("anomaly", "warning")
    assert eng.on_score(KEY, 600, score(9.0), OK) == []  # within cooldown
    eng.on_score(KEY, 700, score(0.0), OK)  # back inside resets the run
    (serious,) = eng.on_score(KEY, 2_000, score(9.0), OK)  # far outside, first miss
    assert serious.severity == "serious"


def test_regime_accuracy_alert_needs_a_sustained_drop():
    eng = AlertEngine(sustain_ms=30_000)
    assert eng.on_accuracy("StationA", 0, 0.5, 1_000) == []
    assert eng.on_accuracy("StationA", 10_000, 0.9, 1_000) == []  # blip recovered
    assert eng.on_accuracy("StationA", 20_000, 0.5, 1_000) == []
    (alert,) = eng.on_accuracy("StationA", 50_000, 0.6, 1_000)
    assert (alert.rule, alert.state, alert.sensor) == ("regime_accuracy", "firing", None)
    (ok,) = eng.on_accuracy("StationA", 60_000, 0.8, 1_000)
    assert ok.state == "resolved"


def test_calibration_alert_has_hysteresis():
    eng = AlertEngine()
    low = HorizonMetrics(horizon=1, n=150, mae=0.1, mase=0.95, coverage=0.90)
    mid = HorizonMetrics(horizon=1, n=150, mae=0.1, mase=0.95, coverage=0.94)
    (fired,) = eng.on_score(KEY, 0, score(0.0), low)
    assert (fired.rule, fired.state, fired.severity) == ("calibration", "firing", "serious")
    assert eng.on_score(KEY, 1, score(0.0), low) == []  # already firing
    assert eng.on_score(KEY, 2, score(0.0), mid) == []  # above floor, below recovery margin
    (resolved,) = eng.on_score(KEY, 3, score(0.0), OK)
    assert (resolved.state, resolved.severity) == ("resolved", "info")


def test_model_degraded_when_worse_than_persistence():
    eng = AlertEngine()
    bad = HorizonMetrics(horizon=1, n=150, mae=0.1, mase=1.3, coverage=0.98)
    (alert,) = eng.on_score(KEY, 0, score(0.0), bad)
    assert alert.rule == "model_degraded"


def test_rules_wait_for_enough_scored_windows():
    eng = AlertEngine(min_scored=100)
    few = HorizonMetrics(horizon=1, n=10, mae=0.1, mase=2.0, coverage=0.5)
    assert eng.on_score(KEY, 0, score(0.0), few) == []


def test_regime_change_needs_persistence_and_skips_the_first_regime():
    eng = AlertEngine(regime_persist=3)
    assert all(eng.on_regime("StationA", t, 0) == [] for t in range(3))  # first regime confirmed
    assert eng.on_regime("StationA", 10, 2) == []
    assert eng.on_regime("StationA", 11, 0) == []  # blip resets the candidate
    out = [eng.on_regime("StationA", t, 2) for t in range(20, 23)]
    assert out[:2] == [[], []]
    (alert,) = out[2]
    assert alert.rule == "regime_change" and "R0 \u2192 R2" in alert.message
