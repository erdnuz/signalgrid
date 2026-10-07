import numpy as np
import pytest

from signalgrid_forecast.config import Settings
from signalgrid_forecast.models import RawEvent
from signalgrid_forecast.regime import RegimeDetector, RegimeScorer, fit_msvar
from signalgrid_forecast.service import ForecastService
from simulator import SIGMA, simulate


@pytest.fixture(scope="module")
def data():
    return simulate(5_000, seed=0)


@pytest.fixture(scope="module")
def fitted(data):
    levels, _ = data
    return fit_msvar(levels[:2_001])


def test_fit_recovers_regime_volatilities_in_canonical_order(fitted):
    params, _ = fitted
    vols = sorted(SIGMA[:, 0])  # canonical order is ascending volatility
    est = [float(np.sqrt(np.diag(c)).mean()) for c in params.cov]
    assert est == sorted(est)
    assert np.allclose(est, vols, atol=0.02)
    assert np.allclose(params.P.sum(axis=1), 1.0)
    assert np.all(np.diag(params.P) > 0.95)  # regimes are persistent


def test_online_filter_tracks_true_regime(data, fitted):
    levels, regimes = data
    params, _ = fitted
    det = RegimeDetector()
    scorer = RegimeScorer()
    for x in levels[:2_001]:
        det.update(x)
    det.apply_fit(params)
    for x, truth in zip(levels[2_001:], regimes[2_001:], strict=True):
        probs = det.update(x)
        assert probs is not None and np.isclose(probs.sum(), 1.0)
        scorer.score(int(np.argmax(probs)), int(truth))
    # Out-of-sample, causal (filtered, not smoothed) accuracy.
    assert scorer.accuracy is not None and scorer.accuracy > 0.85


def test_detector_waits_for_enough_data_before_fitting():
    det = RegimeDetector(min_obs=50, refit_every=20)
    for i in range(50):
        assert det.update([float(i)] * 4) is None
    assert not det.needs_refit
    det.update([0.0] * 4)
    assert det.needs_refit
    det.snapshot()
    assert det.needs_refit  # still never fitted


def test_scorer_finds_best_relabelling():
    scorer = RegimeScorer(k=3, window=100)
    relabel = {0: 2, 1: 0, 2: 1}
    for truth in [0] * 30 + [1] * 30 + [2] * 30:
        state = next(s for s, t in relabel.items() if t == truth)
        scorer.score(state, truth)
    assert scorer.accuracy == 1.0
    assert all(scorer.map(s) == t for s, t in relabel.items())


def test_scorer_window_evicts_old_pairs():
    scorer = RegimeScorer(k=2, window=10)
    for _ in range(10):
        scorer.score(0, 1)  # always wrong under identity...
    for _ in range(10):
        scorer.score(0, 0)
    assert scorer.n == 10
    assert scorer.accuracy == 1.0


def test_service_publishes_throttled_regime_messages(data, fitted):
    levels, regimes = data
    settings = Settings(nats_url="nats://unused:4222", regime_publish_every=5)
    svc = ForecastService(settings)
    det = svc.detector("StationA")
    for x in levels[:700]:
        det.update(x)
    det.apply_fit(fitted[0])

    out = [
        svc.handle_raw(RawEvent(station="StationA", timestamp=i, values=list(x), regime=int(r)))[0]
        for i, (x, r) in enumerate(zip(levels[700:800], regimes[700:800], strict=True))
    ]
    messages = [m for m in out if m is not None]
    assert len(messages) == 20
    last = messages[-1]
    assert last.n_scored == 100
    assert last.mapped_state in {0, 1, 2}
    assert len(last.probabilities) == 3
