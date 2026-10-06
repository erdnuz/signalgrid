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

    The h-step point forecast is written in the regression's own parameters,

        xhat_h = c * S_h + phi^h * x_T,     S_h = sum_{j<h} phi^j,

    so it stays well defined at a unit root (phi -> 1), where the long-run
    mean mu = c / (1 - phi) is not. Interval variance is

        sigma^2 * sum_{j<h} phi^(2j)          (innovations over h steps)
      + g' Cov(c, phi) g                      (parameter uncertainty, delta method)

    with g = d xhat_h / d(c, phi) and Cov(c, phi) = sigma^2 (X'X)^-1 the full
    OLS covariance. For h = 1 this is exactly the textbook prediction variance
    sigma^2 (1 + 1/n + (x_T - xbar)^2 / Sxx).

    (Parameterising by (phi, mu) and ignoring their covariance, as an earlier
    version did, makes the interval explode whenever phi-hat lands near 1:
    mu-hat runs off to infinity and the (x_T - mu) * se_phi term with it.)
    """
    if len(values) < MIN_POINTS:
        return None, None

    x = np.asarray(values, dtype=float)
    x_t, x_tp1 = x[:-1], x[1:]
    n = len(x_t)

    xbar = x_t.mean()
    sxx = np.sum((x_t - xbar) ** 2)
    if not np.isfinite(sxx) or sxx == 0:
        return None, None

    phi = float(np.sum((x_t - xbar) * (x_tp1 - x_tp1.mean())) / sxx)
    c = float(x_tp1.mean() - phi * xbar)
    resid = x_tp1 - (c + phi * x_t)
    sigma2 = float(np.sum(resid**2) / (n - 2))
    if not (np.isfinite(phi) and np.isfinite(sigma2)):
        return None, None

    # sigma^2 (X'X)^-1 for X = [1, x_t], in closed form.
    cov = sigma2 * np.array([[1 / n + xbar**2 / sxx, -xbar / sxx], [-xbar / sxx, 1 / sxx]])
    t_val = float(t.ppf(1 - alpha / 2, df=n - 2))

    last_val = float(x[-1])
    forecasts: list[float] = []
    ci_list: list[tuple[float, float]] = []
    powers = phi ** np.arange(n_steps + 1)  # phi^0 .. phi^H
    for h in range(1, n_steps + 1):
        s_h = powers[:h].sum()
        ds_h = np.sum(np.arange(1, h) * powers[: h - 1])  # d S_h / d phi
        point = c * s_h + powers[h] * last_val

        grad = np.array([s_h, c * ds_h + h * powers[h - 1] * last_val])
        innovation_var = sigma2 * np.sum(powers[:h] ** 2)
        var_h = innovation_var + float(grad @ cov @ grad)
        if not np.isfinite(var_h) or var_h < 0:
            return None, None

        half_width = t_val * np.sqrt(var_h)
        forecasts.append(float(point))
        ci_list.append((float(point - half_width), float(point + half_width)))

    return forecasts, ci_list
