use forge::{aggregate_stats, Event, AGGREGATION_WINDOW_MS};

#[test]
fn aggregate_stats_handles_multiple_stations_and_sensors() {
    let now = 5_000;
    let events = vec![
        Event {
            timestamp: now - 100,
            values: vec![1.0, 2.0],
            station: "A".to_string(),
        },
        Event {
            timestamp: now - 80,
            values: vec![3.0, 4.0],
            station: "A".to_string(),
        },
        Event {
            timestamp: now - 50,
            values: vec![10.0],
            station: "B".to_string(),
        },
    ];

    let out = aggregate_stats(&events, now);
    assert_eq!(out.len(), 3);
}

#[test]
fn aggregate_stats_ignores_old_events() {
    let now = 10_000;
    let events = vec![
        Event {
            timestamp: now - AGGREGATION_WINDOW_MS - 10,
            values: vec![99.0],
            station: "A".to_string(),
        },
        Event {
            timestamp: now - 1,
            values: vec![2.0],
            station: "A".to_string(),
        },
    ];

    let out = aggregate_stats(&events, now);
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].count, 1);
}
