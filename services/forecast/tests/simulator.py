"""NumPy replica of the `pulse` simulator (services/pulse/src/simulator.rs),
used to test regime detection on data with known ground truth."""

from __future__ import annotations

import numpy as np

THETA = np.array([[0.01] * 4, [0.02] * 4, [0.015] * 4])
MU = np.array([[0, 0, 0, 0], [1, 2, 1, 2], [-2, -2, -1, -1]], dtype=float)
SIGMA = np.array([[0.2] * 4, [0.15] * 4, [0.25] * 4])
TRANSITIONS = np.array([[0.995, 0.002, 0.003], [0.004, 0.992, 0.004], [0.005, 0.0025, 0.9925]])


def _random_cholesky(rng: np.random.Generator) -> np.ndarray:
    while True:
        corr = np.eye(4)
        iu = np.triu_indices(4, 1)
        corr[iu] = rng.uniform(-0.3, 0.3, len(iu[0]))
        corr = np.triu(corr) + np.triu(corr, 1).T
        try:
            return np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            continue


def simulate(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Returns levels (n, 4) and true regimes (n,)."""
    rng = np.random.default_rng(seed)
    chols = [_random_cholesky(rng) for _ in range(3)]
    x = MU[0].copy()
    regime = 0
    levels, regimes = [], []
    for _ in range(n):
        shock = chols[regime] @ rng.standard_normal(4)
        x = x + THETA[regime] * (MU[regime] - x) + SIGMA[regime] * shock
        levels.append(x.copy())
        regimes.append(regime)
        regime = int(rng.choice(3, p=TRANSITIONS[regime]))
    return np.array(levels), np.array(regimes)
