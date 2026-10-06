//! Regime-switching multivariate Ornstein-Uhlenbeck sensor simulator.
//!
//! Each step applies an Euler-discretised OU update per channel with
//! cross-channel correlated Gaussian shocks, then samples the next regime
//! from a Markov transition matrix.

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use rand_distr::StandardNormal;

pub const N_REGIMES: usize = 3;
pub const N_CHANNELS: usize = 4;
const CORR_RANGE: f64 = 0.3;

type ChannelParams = [[f64; N_CHANNELS]; N_REGIMES];
type Matrix = [[f64; N_CHANNELS]; N_CHANNELS];

const OU_THETA: ChannelParams = [
    [0.01, 0.01, 0.01, 0.01],
    [0.02, 0.02, 0.02, 0.02],
    [0.015, 0.015, 0.015, 0.015],
];

const OU_MU: ChannelParams = [
    [0.0, 0.0, 0.0, 0.0],
    [1.0, 2.0, 1.0, 2.0],
    [-2.0, -2.0, -1.0, -1.0],
];

const OU_SIGMA: ChannelParams = [
    [0.2, 0.2, 0.2, 0.2],
    [0.15, 0.15, 0.15, 0.15],
    [0.25, 0.25, 0.25, 0.25],
];

pub const TRANSITION_MATRIX: [[f64; N_REGIMES]; N_REGIMES] = [
    [0.995, 0.002, 0.003],
    [0.004, 0.992, 0.004],
    [0.005, 0.0025, 0.9925],
];

pub struct IoTSensorSimulator {
    pub n_channels: usize,
    pub regime_idx: usize,
    /// Target correlation matrix per regime.
    corr: [Matrix; N_REGIMES],
    /// Lower-triangular Cholesky factor of `corr`, used to mix i.i.d. shocks.
    chol: [Matrix; N_REGIMES],
    state: [f64; N_CHANNELS],
    rng: StdRng,
}

impl Default for IoTSensorSimulator {
    fn default() -> Self {
        Self::new()
    }
}

impl IoTSensorSimulator {
    pub fn new() -> Self {
        Self::with_rng(StdRng::from_os_rng())
    }

    pub fn with_seed(seed: u64) -> Self {
        Self::with_rng(StdRng::seed_from_u64(seed))
    }

    fn with_rng(mut rng: StdRng) -> Self {
        let mut corr = [[[0.0; N_CHANNELS]; N_CHANNELS]; N_REGIMES];
        let mut chol = corr;
        for r in 0..N_REGIMES {
            // Resample until positive definite. With |rho| < 0.3 and 4 channels
            // this almost always succeeds on the first draw.
            loop {
                let candidate = random_correlation(&mut rng);
                if let Some(l) = cholesky(&candidate) {
                    corr[r] = candidate;
                    chol[r] = l;
                    break;
                }
            }
        }

        Self {
            n_channels: N_CHANNELS,
            regime_idx: 0,
            corr,
            chol,
            state: OU_MU[0],
            rng,
        }
    }

    pub fn correlation(&self, regime: usize) -> &Matrix {
        &self.corr[regime]
    }

    pub fn step(&mut self) -> Vec<f64> {
        let r = self.regime_idx;

        let z: [f64; N_CHANNELS] = std::array::from_fn(|_| self.rng.sample(StandardNormal));
        let l = &self.chol[r];
        for (i, (x, l_row)) in self.state.iter_mut().zip(l).enumerate() {
            // Lower-triangular: only l[i][0..=i] is non-zero.
            let shock: f64 = l_row[..=i].iter().zip(&z).map(|(l, z)| l * z).sum();
            *x += OU_THETA[r][i] * (OU_MU[r][i] - *x) + OU_SIGMA[r][i] * shock;
        }

        let u: f64 = self.rng.random();
        let mut cumulative = 0.0;
        for (next, &p) in TRANSITION_MATRIX[r].iter().enumerate() {
            cumulative += p;
            if u < cumulative {
                if next != r {
                    tracing::info!(
                        service = "pulse",
                        event = "regime_change",
                        from = r,
                        to = next
                    );
                }
                self.regime_idx = next;
                break;
            }
        }

        self.state.to_vec()
    }
}

fn random_correlation(rng: &mut StdRng) -> Matrix {
    let mut m = [[0.0; N_CHANNELS]; N_CHANNELS];
    for i in 0..N_CHANNELS {
        m[i][i] = 1.0;
        for j in (i + 1)..N_CHANNELS {
            let v = rng.random_range(-CORR_RANGE..CORR_RANGE);
            m[i][j] = v;
            m[j][i] = v;
        }
    }
    m
}

/// Cholesky decomposition `a = l * l^T`; `None` if `a` is not positive definite.
pub fn cholesky(a: &Matrix) -> Option<Matrix> {
    let mut l = [[0.0; N_CHANNELS]; N_CHANNELS];
    for i in 0..N_CHANNELS {
        for j in 0..=i {
            let sum: f64 = (0..j).map(|k| l[i][k] * l[j][k]).sum();
            if i == j {
                let d = a[i][i] - sum;
                if d <= 0.0 {
                    return None;
                }
                l[i][j] = d.sqrt();
            } else {
                l[i][j] = (a[i][j] - sum) / l[j][j];
            }
        }
    }
    Some(l)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simulator_initializes_expected_shape() {
        let simulator = IoTSensorSimulator::with_seed(1);
        assert_eq!(simulator.n_channels, N_CHANNELS);
        assert_eq!(simulator.regime_idx, 0);
    }

    #[test]
    fn step_keeps_regime_in_valid_range() {
        let mut simulator = IoTSensorSimulator::with_seed(2);
        for _ in 0..1_000 {
            let sample = simulator.step();
            assert_eq!(sample.len(), N_CHANNELS);
            assert!(sample.iter().all(|v| v.is_finite()));
            assert!(simulator.regime_idx < N_REGIMES);
        }
    }

    #[test]
    fn cholesky_reconstructs_input() {
        let mut rng = StdRng::seed_from_u64(3);
        let a = random_correlation(&mut rng);
        let l = cholesky(&a).expect("positive definite");
        for i in 0..N_CHANNELS {
            for j in 0..N_CHANNELS {
                let v: f64 = (0..N_CHANNELS).map(|k| l[i][k] * l[j][k]).sum();
                assert!((v - a[i][j]).abs() < 1e-12);
            }
        }
    }

    #[test]
    fn cholesky_rejects_non_positive_definite() {
        let mut a = [[0.0; N_CHANNELS]; N_CHANNELS];
        for (i, row) in a.iter_mut().enumerate() {
            row[i] = 1.0;
        }
        a[0][1] = 1.5;
        a[1][0] = 1.5;
        assert!(cholesky(&a).is_none());
    }

    /// The mixed shocks must reproduce the target correlation (the original
    /// implementation produced C*C^T instead of C).
    #[test]
    fn shocks_have_target_correlation() {
        let mut sim = IoTSensorSimulator::with_seed(4);
        let l = sim.chol[0];
        let target = sim.corr[0];
        let n = 200_000;
        let mut acc = [[0.0; N_CHANNELS]; N_CHANNELS];
        for _ in 0..n {
            let z: [f64; N_CHANNELS] = std::array::from_fn(|_| sim.rng.sample(StandardNormal));
            let x: [f64; N_CHANNELS] =
                std::array::from_fn(|i| (0..=i).map(|j| l[i][j] * z[j]).sum());
            for i in 0..N_CHANNELS {
                for j in 0..N_CHANNELS {
                    acc[i][j] += x[i] * x[j];
                }
            }
        }
        for i in 0..N_CHANNELS {
            for j in 0..N_CHANNELS {
                let emp = acc[i][j] / n as f64;
                assert!(
                    (emp - target[i][j]).abs() < 0.02,
                    "({i},{j}) {emp} vs {}",
                    target[i][j]
                );
            }
        }
    }
}
