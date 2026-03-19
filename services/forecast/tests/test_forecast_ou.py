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
