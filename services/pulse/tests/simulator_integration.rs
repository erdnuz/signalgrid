use pulse::simulator::{IoTSensorSimulator, N_CHANNELS};

#[test]
fn seeded_simulators_are_deterministic() {
    let mut a = IoTSensorSimulator::with_seed(42);
    let mut b = IoTSensorSimulator::with_seed(42);
    for _ in 0..500 {
        assert_eq!(a.step(), b.step());
        assert_eq!(a.regime_idx, b.regime_idx);
    }
}

#[test]
fn simulator_generates_finite_values_over_time() {
    let mut simulator = IoTSensorSimulator::with_seed(7);
    for _ in 0..10_000 {
        let sample = simulator.step();
        assert_eq!(sample.len(), N_CHANNELS);
        assert!(sample.iter().all(|v| v.is_finite()));
    }
}
