import pathlib
import sys
import importlib.util

import numpy as np

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("forecast_main", MODULE_PATH)
forecast_main = importlib.util.module_from_spec(SPEC)
sys.modules["forecast_main"] = forecast_main
SPEC.loader.exec_module(forecast_main)


def test_forecast_ou_returns_none_for_insufficient_points():
    values = [1.0] * (forecast_main.MIN_POINTS - 1)
    forecasts, ci = forecast_main.forecast_ou(values)
    assert forecasts is None
    assert ci is None


def test_forecast_ou_returns_none_for_constant_series():
    values = [42.0] * forecast_main.MIN_POINTS
    forecasts, ci = forecast_main.forecast_ou(values)
    assert forecasts is None
    assert ci is None


def test_forecast_ou_returns_horizon_and_finite_bounds_for_valid_series():
    values = [
        float(10.0 + 0.05 * i + 0.2 * np.sin(i / 3.0))
        for i in range(forecast_main.MIN_POINTS + 10)
    ]

    forecasts, ci = forecast_main.forecast_ou(
        values,
        n_steps=forecast_main.FORECAST_HORIZON,
    )

    assert forecasts is not None
    assert ci is not None
    assert len(forecasts) == forecast_main.FORECAST_HORIZON
    assert len(ci) == forecast_main.FORECAST_HORIZON

    for lower, upper in ci:
        assert np.isfinite(lower)
        assert np.isfinite(upper)
        assert lower <= upper


def _simulate_ar1(phi, mu, sigma, n, seed):
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = mu
    for i in range(1, n):
        x[i] = mu + phi * (x[i - 1] - mu) + sigma * rng.standard_normal()
    return x


def test_forecast_ou_recovers_mean_reversion():
    values = _simulate_ar1(phi=0.8, mu=5.0, sigma=0.1, n=400, seed=0)
    forecasts, _ = forecast_main.forecast_ou(list(values), n_steps=50)
    # Long-horizon forecast converges to the fitted mean.
    assert abs(forecasts[-1] - 5.0) < 0.1


def test_forecast_ou_interval_coverage_close_to_nominal():
    # Empirical 1-step coverage of the 98% interval over many independent series.
    rng = np.random.default_rng(1)
    hits, trials = 0, 400
    for _ in range(trials):
        series = _simulate_ar1(phi=0.7, mu=0.0, sigma=1.0, n=forecast_main.MIN_POINTS * 3 + 1, seed=rng.integers(1 << 32))
        history, actual = series[:-1], series[-1]
        _, ci = forecast_main.forecast_ou(list(history), n_steps=1)
        lower, upper = ci[0]
        hits += lower <= actual <= upper
    coverage = hits / trials
    assert 0.94 <= coverage <= 1.0


def test_forecast_ou_interval_widens_with_horizon():
    values = _simulate_ar1(phi=0.9, mu=0.0, sigma=1.0, n=100, seed=3)
    _, ci = forecast_main.forecast_ou(list(values), n_steps=5)
    widths = [upper - lower for lower, upper in ci]
    assert widths == sorted(widths)
