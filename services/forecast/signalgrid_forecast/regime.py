"""Online regime detection with a Markov-switching VAR(1).

Model, for regime k and increments y_t = x_t - x_{t-1} of the d-channel raw
signal:

    y_t | x_{t-1}, s_t = k  ~  N(a_k + b_k * x_{t-1}, Sigma_k)     (b_k diagonal)
    P(s_t = j | s_{t-1} = i) = P_ij

This is the exact form of an Euler-discretised OU process whose drift,
volatility and correlation switch with a hidden Markov chain, which is what
`pulse` simulates (a_k = theta_k * mu_k, b_k = -theta_k).

* Parameters are fitted **without labels** by Baum-Welch EM on a rolling
  window, refitted periodically off the event loop.
* Between refits, each new sample updates the filtered probabilities
  P(s_t | x_1..t) with one step of the Hamilton filter (O(K^2 + K d^2)).
* Hidden states are unordered, so fitted states are put in a canonical order
  (ascending total volatility, trace Sigma_k), which stays stable across
  refits. Mapping canonical states to the simulator's ground-truth labels is
  done only for *evaluation* (`RegimeScorer`), using the Hungarian assignment
  on a rolling confusion matrix, the standard way to score unsupervised
  clustering.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment

Array = NDArray[np.float64]

_LOG_2PI = float(np.log(2 * np.pi))


@dataclass(frozen=True)
class MSVarParams:
    pi: Array  # (K,)   initial state distribution
    P: Array  # (K, K) transition matrix, rows sum to 1
    a: Array  # (K, d) intercepts
    b: Array  # (K, d) per-channel mean-reversion coefficients
    cov: Array  # (K, d, d) innovation covariances

    @property
    def k(self) -> int:
        return int(self.pi.shape[0])

    def log_emission(self, y: Array, x_prev: Array) -> Array:
        """(T, d), (T, d) -> (T, K) log N(y_t; a_k + b_k x_{t-1}, Sigma_k)."""
        y2 = np.atleast_2d(y)
        xp = np.atleast_2d(x_prev)
        d = y2.shape[1]
        out = np.empty((y2.shape[0], self.k))
        for k in range(self.k):
            resid = y2 - (self.a[k] + self.b[k] * xp)
            chol = np.linalg.cholesky(self.cov[k])
            z = np.linalg.solve(chol, resid.T)  # (d, T)
            logdet = 2.0 * np.log(np.diag(chol)).sum()
            out[:, k] = -0.5 * (d * _LOG_2PI + logdet + (z * z).sum(axis=0))
        return out

    def reordered(self, order: NDArray[np.intp]) -> MSVarParams:
        return MSVarParams(
            pi=self.pi[order],
            P=self.P[np.ix_(order, order)],
            a=self.a[order],
            b=self.b[order],
            cov=self.cov[order],
        )


def _forward_backward(log_b: Array, pi: Array, P: Array) -> tuple[Array, Array, Array, float]:
    """Scaled forward-backward. Returns (filtered alpha, smoothed gamma,
    expected transition counts, log-likelihood)."""
    T, K = log_b.shape
    shift = log_b.max(axis=1, keepdims=True)
    B = np.exp(log_b - shift)

    alpha = np.empty((T, K))
    c = np.empty(T)
    a0 = pi * B[0]
    c[0] = a0.sum()
    alpha[0] = a0 / c[0]
    for t in range(1, T):
        at = (alpha[t - 1] @ P) * B[t]
        c[t] = at.sum()
        alpha[t] = at / c[t]

    beta = np.empty((T, K))
    beta[-1] = 1.0
    xi = np.zeros((K, K))
    for t in range(T - 2, -1, -1):
        bb = B[t + 1] * beta[t + 1]
        beta[t] = (P @ bb) / c[t + 1]
        xi += np.outer(alpha[t], bb) * P / c[t + 1]

    gamma = alpha * beta
    gamma /= gamma.sum(axis=1, keepdims=True)
    loglik = float(np.log(c).sum() + shift.sum())
    return alpha, gamma, xi, loglik


def _m_step(y: Array, x_prev: Array, gamma: Array, xi: Array, ridge: float) -> MSVarParams:
    d = y.shape[1]
    K = gamma.shape[1]
    a = np.empty((K, d))
    b = np.empty((K, d))
    cov = np.empty((K, d, d))
    for k in range(K):
        w = gamma[:, k]
        sw = w.sum() + 1e-12
        # Closed-form weighted least squares of y[:, c] on [1, x_prev[:, c]] per channel.
        sx = w @ x_prev
        sxx = w @ (x_prev * x_prev)
        sy = w @ y
        sxy = w @ (x_prev * y)
        det = sw * sxx - sx * sx + ridge
        b[k] = (sw * sxy - sx * sy) / det
        a[k] = (sy - b[k] * sx) / sw
        resid = y - (a[k] + b[k] * x_prev)
        cov[k] = (resid * w[:, None]).T @ resid / sw + ridge * np.eye(d)
    P = xi / xi.sum(axis=1, keepdims=True)
    pi = gamma[0] / gamma[0].sum()
    return MSVarParams(pi=pi, P=P, a=a, b=b, cov=cov)


def _initial_responsibilities(y: Array, k: int, smooth: int = 25) -> Array:
    """Seed EM by splitting the series into k groups of local volatility."""
    energy = (y * y).sum(axis=1)
    kernel = np.ones(smooth) / smooth
    local_vol = np.convolve(energy, kernel, mode="same")
    edges = np.quantile(local_vol, np.linspace(0, 1, k + 1)[1:-1])
    labels = np.searchsorted(edges, local_vol)
    gamma = np.full((len(y), k), 0.05 / max(k - 1, 1))
    gamma[np.arange(len(y)), labels] = 0.95
    return gamma


def _em(
    y: Array, x_prev: Array, params: MSVarParams, max_iter: int, tol: float, ridge: float
) -> tuple[MSVarParams, float]:
    prev = -np.inf
    loglik = prev
    for _ in range(max_iter):
        _, gamma, xi, loglik = _forward_backward(params.log_emission(y, x_prev), params.pi, params.P)
        params = _m_step(y, x_prev, gamma, xi, ridge)
        if loglik - prev < tol * len(y):
            break
        prev = loglik
    return params, loglik


def fit_msvar(
    x: Array,
    k: int = 3,
    init: MSVarParams | None = None,
    smoothings: tuple[int, ...] = (10, 25, 60),
    max_iter: int = 40,
    tol: float = 1e-5,
    ridge: float = 1e-8,
) -> tuple[MSVarParams, float]:
    """Fits the model to levels `x` of shape (T + 1, d) by EM.

    EM only finds a local optimum, so it is restarted from several
    volatility-based initialisations (plus `init`, typically the previous
    fit) and the highest-likelihood solution wins. Returns parameters in
    canonical order and the final log-likelihood."""
    x = np.asarray(x, dtype=float)
    y = np.diff(x, axis=0)
    x_prev = x[:-1]
    T = len(y)

    off = 0.01 / max(k - 1, 1)
    sticky = np.full((k, k), off) + np.eye(k) * (0.99 - off)
    starts = [init] if init is not None else []
    starts += [
        _m_step(y, x_prev, _initial_responsibilities(y, k, smooth), sticky * T / k, ridge)
        for smooth in smoothings
    ]

    best: tuple[MSVarParams, float] | None = None
    for start in starts:
        params, loglik = _em(y, x_prev, start, max_iter, tol, ridge)
        if best is None or loglik > best[1]:
            best = (params, loglik)
    assert best is not None
    params, loglik = best
    order = np.argsort([np.trace(c) for c in params.cov])
    return params.reordered(order), loglik


class RegimeDetector:
    """Rolling-window EM + online Hamilton filter for one station."""

    def __init__(self, k: int = 3, window: int = 3_000, min_obs: int = 1_200, refit_every: int = 600):
        self.k = k
        self.window = window
        self.min_obs = min_obs
        self.refit_every = refit_every
        self.params: MSVarParams | None = None
        self._levels: deque[Array] = deque(maxlen=window + 1)
        self._since_fit = 0
        self._alpha: Array | None = None

    @property
    def needs_refit(self) -> bool:
        enough = len(self._levels) > self.min_obs
        return enough and (self.params is None or self._since_fit >= self.refit_every)

    def snapshot(self) -> Array:
        """Copy of the training window, safe to hand to another thread."""
        self._since_fit = 0
        return np.array(self._levels)

    def apply_fit(self, params: MSVarParams, warmup: int = 300) -> None:
        """Installs new parameters and re-filters recent samples so the
        probabilities reflect them immediately."""
        self.params = params
        recent = np.array(list(self._levels)[-(warmup + 1) :])
        y = np.diff(recent, axis=0)
        alpha, _, _, _ = _forward_backward(
            params.log_emission(y, recent[:-1]), np.full(self.k, 1.0 / self.k), params.P
        )
        self._alpha = alpha[-1]

    def update(self, values: list[float] | Array) -> Array | None:
        """Adds one sample; returns filtered state probabilities once fitted."""
        x = np.asarray(values, dtype=float)
        prev = self._levels[-1] if self._levels else None
        self._levels.append(x)
        self._since_fit += 1
        if self.params is None or prev is None or self._alpha is None:
            return None
        log_b = self.params.log_emission(x - prev, prev)[0]
        pred = self._alpha @ self.params.P
        post = pred * np.exp(log_b - log_b.max())
        total = post.sum()
        # Guard against a numerically dead update (e.g. an extreme outlier).
        self._alpha = post / total if np.isfinite(total) and total > 0 else pred
        return self._alpha


class RegimeScorer:
    """Rolling accuracy of detected (canonical) states against ground truth,
    under the best one-to-one relabelling."""

    def __init__(self, k: int = 3, window: int = 3_000):
        self.k = k
        self._pairs: deque[tuple[int, int]] = deque(maxlen=window)
        self._confusion = np.zeros((k, k), dtype=np.int64)
        self._mapping = np.arange(k)

    def score(self, state: int, truth: int) -> None:
        if len(self._pairs) == self._pairs.maxlen:
            old_state, old_truth = self._pairs[0]
            self._confusion[old_state, old_truth] -= 1
        self._pairs.append((state, truth))
        self._confusion[state, truth] += 1
        rows, cols = linear_sum_assignment(-self._confusion)
        self._mapping[rows] = cols

    def map(self, state: int) -> int:
        return int(self._mapping[state])

    @property
    def n(self) -> int:
        return len(self._pairs)

    @property
    def accuracy(self) -> float | None:
        if not self._pairs:
            return None
        matched = self._confusion[np.arange(self.k), self._mapping].sum()
        return float(matched / len(self._pairs))
