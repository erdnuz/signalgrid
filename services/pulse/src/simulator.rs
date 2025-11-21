use rand_distr::{Normal, Distribution};
use rand::Rng;

pub struct IoTSensorSimulator {
    pub n_channels: usize,
    pub n_main: usize,
    pub n_regimes: usize,     // n_main + transition regimes
    pub regime_idx: usize,
    ou_theta: Vec<Vec<f64>>,   // [regime][channel]
    ou_mu: Vec<Vec<f64>>,
    ou_sigma: Vec<Vec<f64>>,
    corr_matrix: Vec<Vec<Vec<f64>>>, // [regime][i][j]
    transition_matrix: Vec<Vec<f64>>, // [regime_from][regime_to]
    event_rate: Vec<f64>, // per regime
    event_magnitude: Vec<f64>, // per regime
    state: Vec<f64>,
}

impl IoTSensorSimulator {
    pub fn new() -> Self {
        let n_main = 3;
        let n_channels = 4;
        let n_regimes = n_main * 2; // main + transition
        let mut rng = rand::rng();

        // OU parameters per regime (main + transition)
        let ou_theta = vec![
            vec![0.01, 0.02, 0.015, 0.01], // R0
            vec![0.02, 0.01, 0.02, 0.015], // R1
            vec![0.03, 0.025, 0.02, 0.02], // R2
            vec![0.02, 0.02, 0.018, 0.015], // T0
            vec![0.025, 0.015, 0.02, 0.018], // T1
            vec![0.03, 0.02, 0.022, 0.02], // T2
        ];

        let ou_mu = vec![
            vec![20.0, 0.0, 1013.0, 5.0], 
            vec![25.0, -2.0, 1010.0, 6.0],
            vec![18.0, 1.0, 1015.0, 4.5],
            vec![22.0, -1.0, 1012.0, 5.5], // T0
            vec![24.0, 0.0, 1011.0, 5.8], // T1
            vec![19.0, 1.5, 1014.0, 4.8], // T2
        ];

        let ou_sigma = vec![
            vec![0.5, 0.6, 0.3, 0.1],
            vec![0.6, 0.7, 0.4, 0.2],
            vec![0.4, 0.5, 0.2, 0.1],
            vec![0.8, 0.9, 0.4, 0.9], // T0
            vec![0.9, 1.0, 0.5, 0.8], // T1
            vec![0.6, 0.7, 0.6, 0.5], // T2
        ];

        // Correlation matrices
        let mut corr_matrix = vec![vec![vec![0.0; n_channels]; n_channels]; n_regimes];
        for r in 0..n_regimes {
            for i in 0..n_channels {
                corr_matrix[r][i][i] = 1.0; // diagonal
                for j in (i+1)..n_channels {
                    let value = rng.random_range(0.3..0.9);
                    corr_matrix[r][i][j] = value;
                    corr_matrix[r][j][i] = value; // mirror
                }
            }
        }

        // Transition matrix: main -> self or transition, transition -> next main
        let mut transition_matrix = vec![vec![0.0; n_regimes]; n_regimes];

        // --- Main regimes: R0..R2 ---
        transition_matrix[0] = vec![
            1.0 - (0.01 + 0.005), // stay in R0
            0.0,                   // cannot go directly to R1
            0.0,                   // cannot go directly to R2
            0.0,                   // cannot go to T0
            0.01,                  // R0 -> T1
            0.005,                 // R0 -> T2
        ];

        transition_matrix[1] = vec![
            0.0,
            1.0 - (0.008 + 0.008), // stay in R1
            0.0,
            0.008,                  // R1 -> T0
            0.008,                  // R1 -> T1
            0.0,
        ];

        transition_matrix[2] = vec![
            0.0,
            0.0,
            1.0 - (0.002 + 0.01), // stay in R2
            0.01,                 // R2 -> T0
            0.002,                // R2 -> T1
            0.0,
        ];

        // --- Transition regimes: T0..T2 ---
        transition_matrix[3] = vec![
            0.05,                // T0 -> R0
            0.0,
            0.0,
            1.0 - 0.05,          // stay in T0
            0.0,
            0.0,
        ];

        transition_matrix[4] = vec![
            0.0,
            0.08,                // T1 -> R1
            0.0,
            0.0,
            1.0 - 0.08,          // stay in T1
            0.0,
        ];

        transition_matrix[5] = vec![
            0.0,
            0.0,
            0.02,                // T2 -> R2
            0.0,
            0.0,
            1.0 - 0.02,          // stay in T2
        ];

        let event_rate = vec![0.001, 0.0025, 0.003, 0.008, 0.01, 0.012];
        let event_magnitude = vec![2.0, 5.0, 3.0, 12.0, 18.0, 15.0];

        let state = ou_mu[0].clone();

        Self {
            n_channels,
            n_main,
            n_regimes,
            regime_idx: 0,
            ou_theta,
            ou_mu,
            ou_sigma,
            corr_matrix,
            transition_matrix,
            event_rate,
            event_magnitude,
            state,
        }
    }


    pub fn step(&mut self) -> Vec<f64> {
        let mut rng = rand::rng();
        let normal = Normal::new(0.0, 1.0).unwrap();

        // --- Generate correlated noise ---
        let mut noise = vec![0.0; self.n_channels];
        for i in 0..self.n_channels {
            noise[i] = normal.sample(&mut rng);
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

        // --- Random burst events per regime ---
        let mut event_triggered = false;
        if rng.random::<f64>() < self.event_rate[self.regime_idx] {
            event_triggered = true; // mark that an event happened
            for i in 0..self.n_channels {
                self.state[i] += self.event_magnitude[self.regime_idx] * (0.5 + rng.random::<f64>());
            }
        }

        if self.regime_idx < self.n_main {
            // Currently in a main regime
            let mut probs = self.transition_matrix[self.regime_idx].clone();
            
            if event_triggered {
                // Multiply only probabilities leading to transition states by 5
                for i in self.n_main..self.n_regimes {
                    probs[i] *= 5.0;
                }
                // Normalize
                let total: f64 = probs.iter().sum();
                for p in probs.iter_mut() {
                    *p /= total;
                }
            }

            // Sample next regime
            let r: f64 = rng.random();
            let mut cumulative = 0.0;
            for (i, &p) in probs.iter().enumerate() {
                cumulative += p;
                if r < cumulative {
                    self.regime_idx = i;
                    break;
                }
            }
        } else {
            // Currently in a transition regime -> follow normal transition matrix
            let probs = &self.transition_matrix[self.regime_idx];
            let r: f64 = rng.random();
            let mut cumulative = 0.0;
            for (i, &p) in probs.iter().enumerate() {
                cumulative += p;
                if r < cumulative {
                    self.regime_idx = i;
                    break;
                }
            }
        }

        self.state.clone()
    }

}
