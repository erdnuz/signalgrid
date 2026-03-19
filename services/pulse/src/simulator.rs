use rand_distr::StandardNormal;
use rand::Rng;

// Named constants to avoid magic numbers and improve readability
const DEFAULT_N_REGIMES: usize = 3;
const DEFAULT_N_CHANNELS: usize = 4;
const DEFAULT_CORR_RANGE: f64 = 0.3;

const DEFAULT_OU_THETA: [[f64; DEFAULT_N_CHANNELS]; DEFAULT_N_REGIMES] = [
    [0.01, 0.01, 0.01, 0.01], // R0
    [0.02, 0.02, 0.02, 0.02], // R1
    [0.015, 0.015, 0.015, 0.015], // R2
];

const DEFAULT_OU_MU: [[f64; DEFAULT_N_CHANNELS]; DEFAULT_N_REGIMES] = [
    [0.0, 0.0, 0.0, 0.0],
    [1.0, 2.0, 1.0, 2.0],
    [-2.0, -2.0, -1.0, -1.0],
];

const DEFAULT_OU_SIGMA: [[f64; DEFAULT_N_CHANNELS]; DEFAULT_N_REGIMES] = [
    [0.2, 0.2, 0.2, 0.2],
    [0.15, 0.15, 0.15, 0.15],
    [0.25, 0.25, 0.25, 0.25],
];

const DEFAULT_TRANSITION_MATRIX: [[f64; DEFAULT_N_REGIMES]; DEFAULT_N_REGIMES] = [
    [0.995, 0.002, 0.003],
    [0.004, 0.992, 0.004],
    [0.005, 0.0025, 0.9925],
];

pub struct IoTSensorSimulator {
    pub n_channels: usize,
    pub regime_idx: usize,
    ou_theta: Vec<Vec<f64>>,   // [regime][channel]
    ou_mu: Vec<Vec<f64>>,
    ou_sigma: Vec<Vec<f64>>,
    corr_matrix: Vec<Vec<Vec<f64>>>, // [regime][i][j]
    transition_matrix: Vec<Vec<f64>>, // [regime_from][regime_to]
    state: Vec<f64>,
}

impl IoTSensorSimulator {
    pub fn new() -> Self {
        let n_regimes = DEFAULT_N_REGIMES;
        let n_channels = DEFAULT_N_CHANNELS;
        let mut rng = rand::rng();

        // OU parameters per regime (main + transition)
        let ou_theta: Vec<Vec<f64>> = DEFAULT_OU_THETA.iter().map(|r| r.to_vec()).collect();
        let ou_mu: Vec<Vec<f64>> = DEFAULT_OU_MU.iter().map(|r| r.to_vec()).collect();
        let ou_sigma: Vec<Vec<f64>> = DEFAULT_OU_SIGMA.iter().map(|r| r.to_vec()).collect();

        // Correlation matrices
        let mut corr_matrix = vec![vec![vec![0.0; n_channels]; n_channels]; n_regimes];
        for r in 0..n_regimes {
            for i in 0..n_channels {
                corr_matrix[r][i][i] = 1.0; // diagonal
                for j in (i+1)..n_channels {
                    let value = rng.random_range(-DEFAULT_CORR_RANGE..DEFAULT_CORR_RANGE);
                    corr_matrix[r][i][j] = value;
                    corr_matrix[r][j][i] = value; // mirror
                }
            }
        }

        // Transition matrix
        let mut transition_matrix = vec![vec![0.0; n_regimes]; n_regimes];
        for r in 0..n_regimes {
            transition_matrix[r] = DEFAULT_TRANSITION_MATRIX[r].to_vec();
        }

        let state = ou_mu[0].clone();

        Self {
            n_channels,
            regime_idx: 0,
            ou_theta,
            ou_mu,
            ou_sigma,
            corr_matrix,
            transition_matrix,
            state,
        }
    }


    pub fn step(&mut self) -> Vec<f64> {
        let mut rng = rand::rng();

        // --- Generate correlated noise using StandardNormal (no fallible constructor) ---
        let mut noise = vec![0.0; self.n_channels];
        for i in 0..self.n_channels {
            noise[i] = rng.sample(StandardNormal);
        }

        let mut correlated_noise = vec![0.0; self.n_channels];
        for i in 0..self.n_channels {
            correlated_noise[i] = 0.0;
            for j in 0..self.n_channels {
                correlated_noise[i] += self.corr_matrix[self.regime_idx][i][j] * noise[j];
            }
        }

        // --- OU update per channel ---
        for i in 0..self.n_channels {
            self.state[i] += self.ou_theta[self.regime_idx][i] * (self.ou_mu[self.regime_idx][i] - self.state[i])
                            + self.ou_sigma[self.regime_idx][i] * correlated_noise[i];
        }


        let probs = self.transition_matrix[self.regime_idx].clone();
        

        // Sample next regime
        let r: f64 = rng.random();
        let mut cumulative = 0.0;
        for (i, &p) in probs.iter().enumerate() {
            cumulative += p;
            if r < cumulative {
                if self.regime_idx != i {
                    println!("Changing regime from {} to {}", self.regime_idx, i);
                }
                
                self.regime_idx = i;
                break;
            }
        }

        self.state.clone()
    }

}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simulator_initializes_expected_shape() {
        let simulator = IoTSensorSimulator::new();
        assert_eq!(simulator.n_channels, 4);
        assert_eq!(simulator.regime_idx, 0);
        assert_eq!(simulator.state.len(), simulator.n_channels);
    }

    #[test]
    fn step_returns_one_value_per_channel() {
        let mut simulator = IoTSensorSimulator::new();
        let sample = simulator.step();
        assert_eq!(sample.len(), simulator.n_channels);
    }

    #[test]
    fn step_keeps_regime_in_valid_range() {
        let mut simulator = IoTSensorSimulator::new();
        for _ in 0..200 {
            let _ = simulator.step();
            assert!(simulator.regime_idx < 3);
        }
    }
}
