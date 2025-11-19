use rand_distr::{Normal, Distribution};
use rand::Rng;

pub struct IoTSensorSimulator {
    n_channels: usize,
    ou_theta: Vec<f64>,
    ou_mu: Vec<f64>,
    ou_sigma: Vec<f64>,
    corr_matrix: Vec<Vec<f64>>,
    event_rate: f64,
    event_magnitude: f64,
    state: Vec<f64>,
}
impl IoTSensorSimulator {
    pub fn new(n_channels: usize) -> Self {
        let ou_theta = vec![0.01, 0.02, 0.015, 0.01][..n_channels].to_vec();
        let ou_mu = vec![20.0, 0.0, 1013.0, 5.0][..n_channels].to_vec();
        let ou_sigma = vec![0.5, 0.8, 0.3, 0.6][..n_channels].to_vec();

        // Simple correlation matrix (1.0 on diagonal, 0.7 elsewhere)
        let mut corr_matrix = vec![vec![0.0; n_channels]; n_channels];
        for i in 0..n_channels {
            for j in 0..n_channels {
                corr_matrix[i][j] = if i == j { 1.0 } else { 0.7 };
            }
        }

        let state = ou_mu.clone();

        Self {
            n_channels,
            ou_theta,
            ou_mu,
            ou_sigma,
            corr_matrix,
            event_rate: 0.0005,
            event_magnitude: 10.0,
            state,
        }
    }

    pub fn step(&mut self) -> Vec<f64> {
        let mut rng = rand::rng();
        let normal = Normal::new(0.0, 1.0).unwrap();

        // Generate independent noise
        let mut noise = vec![0.0; self.n_channels];
        for i in 0..self.n_channels {
            noise[i] = normal.sample(&mut rng);
        }

        // Apply simple correlation
        let correlated_noise: Vec<f64> = (0..self.n_channels)
            .map(|i| (0..self.n_channels).map(|j| self.corr_matrix[i][j] * noise[j]).sum())
            .collect();

        // OU update
        for i in 0..self.n_channels {
            self.state[i] += self.ou_theta[i] * (self.ou_mu[i] - self.state[i])
                             + self.ou_sigma[i] * correlated_noise[i];
        }

        // Random burst events
        if rng.random::<f64>() < self.event_rate {
            let magnitude: Vec<f64> = (0..self.n_channels)
                .map(|_| self.event_magnitude + rng.random::<f64>() * self.event_magnitude * 0.5)
                .collect();
            for i in 0..self.n_channels {
                self.state[i] += magnitude[i];
            }
        }

        self.state.clone()
    }
}