"""AR(1) / discrete Ornstein-Uhlenbeck forecasting with prediction intervals."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.stats import t

MIN_POINTS = 20
CI_ALPHA = 0.02  # two-sided -> 98% interval


def forecast_ou(
    values: Sequence[float], n_steps: int = 3, alpha: float = CI_ALPHA
) -> tuple[list[float], list[tuple[float, float]]] | tuple[None, None]:
    """Fit a discrete OU / AR(1) model x_{t+1} = c + phi * x_t + eps by OLS and
    return h-step forecasts with (1 - alpha) prediction intervals.

    Interval variance = innovation variance accumulated over h steps
    + parameter uncertainty propagated by the delta method:
        d xhat_h / d phi = h * phi^(h-1) * (x_T - mu)
        d xhat_h / d mu  = 1 - phi^h
    (the phi/mu covariance is ignored).
    """
    if len(values) < MIN_POINTS:
        return None, None

    x = np.asarray(values, dtype=float)
    x_t, x_tp1 = x[:-1], x[1:]
    n = len(x_t)

    sxx = np.sum((x_t - x_t.mean()) ** 2)
    if sxx == 0:
        return None, None

    phi = np.sum((x_t - x_t.mean()) * (x_tp1 - x_tp1.mean())) / sxx
    if not np.isfinite(phi) or np.isclose(phi, 1.0) or np.isclose(phi**2, 1.0):
        return None, None

    c = x_tp1.mean() - phi * x_t.mean()
    mu = c / (1 - phi)
    resid = x_tp1 - (c + phi * x_t)
    sigma2 = np.sum(resid**2) / (n - 2)
    if not np.isfinite(sigma2):
        return None, None

    se_phi = np.sqrt(sigma2 / sxx)
    se_mu = np.sqrt(sigma2 / n) / abs(1 - phi)
    t_val = t.ppf(1 - alpha / 2, df=n - 2)

    last_val = x[-1]
    forecasts, ci_list = [], []
    for h in range(1, n_steps + 1):
        point = mu + phi**h * (last_val - mu)
        innovation_var = sigma2 * (1 - phi ** (2 * h)) / (1 - phi**2)
        param_var = (h * phi ** (h - 1) * (last_val - mu) * se_phi) ** 2 + ((1 - phi**h) * se_mu) ** 2
        var_h = innovation_var + param_var
        if not np.isfinite(var_h) or var_h < 0:
            return None, None

        half_width = t_val * np.sqrt(var_h)
        forecasts.append(float(point))
        ci_list.append((float(point - half_width), float(point + half_width)))

    return forecasts, ci_list
