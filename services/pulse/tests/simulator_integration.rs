use pulse::simulator::IoTSensorSimulator;

#[test]
fn simulator_generates_values_over_time() {
    let mut simulator = IoTSensorSimulator::new();

    let mut samples = Vec::new();
    for _ in 0..5 {
        samples.push(simulator.step());
    }

    assert_eq!(samples.len(), 5);
    for sample in samples {
        assert_eq!(sample.len(), simulator.n_channels);
        assert!(sample.iter().all(|v| v.is_finite()));
    }
}
