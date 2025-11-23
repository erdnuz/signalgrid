use rand_distr::{Normal, Distribution};
use rand::Rng;

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
        let n_regimes = 3;
        let n_channels = 4;
        let mut rng = rand::rng();

        // OU parameters per regime (main + transition)
        let ou_theta = vec![
            vec![0.01, 0.01, 0.01, 0.01], // R0
            vec![0.02, 0.02, 0.02, 0.02], // R1
            vec![0.015, 0.015, 0.015, 0.015], // R2
        ];

        let ou_mu = vec![
            vec![0.0, 0.0, 0.0, 0.0], 
            vec![1.0, 2.0, 1.0, 2.0],
            vec![-2.0, -2.0, -1.0, -1.0],
        ];

        let ou_sigma = vec![
            vec![0.2, 0.2, 0.2, 0.2],
            vec![0.15, 0.15, 0.15, 0.15],
            vec![0.25, 0.25, 0.25, 0.25,]
        ];

        // Correlation matrices
        let mut corr_matrix = vec![vec![vec![0.0; n_channels]; n_channels]; n_regimes];
        for r in 0..n_regimes {
            for i in 0..n_channels {
                corr_matrix[r][i][i] = 1.0; // diagonal
                for j in (i+1)..n_channels {
                    let value = rng.random_range(-0.3..0.3);
                    corr_matrix[r][i][j] = value;
                    corr_matrix[r][j][i] = value; // mirror
                }
            }
        }

        // Transition matrix: main -> self or transition, transition -> next main
        let mut transition_matrix = vec![vec![0.0; n_regimes]; n_regimes];

        // --- Main regimes: R0..R2 ---
        transition_matrix[0] = vec![
            0.995,                   // cannot go to T0
            0.002,                  // R0 -> T1
            0.003,                 // R0 -> T2
        ];

        transition_matrix[1] = vec![
            0.004,
            0.992, // stay in R1
            0.004
        ];

        transition_matrix[2] = vec![
            0.005,
            0.0025,
            0.9925
        ];

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


        let mut probs = self.transition_matrix[self.regime_idx].clone();
        

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
