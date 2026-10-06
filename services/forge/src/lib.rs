use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const AGGREGATION_WINDOW_MS: u64 = 600;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Event {
    pub timestamp: u64,
    pub values: Vec<f64>,
    pub station: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Stats {
    pub station: String,
    pub sensor: usize,
    pub mean: f64,
    pub min: f64,
    pub max: f64,
    pub count: usize,
    pub timestamp: u64,
}

/// Drops buffered events that can no longer fall inside any future aggregation window.
pub fn prune_expired(events: &mut Vec<Event>, now_ms: u64) {
    let cutoff_ms = now_ms.saturating_sub(AGGREGATION_WINDOW_MS);
    events.retain(|event| event.timestamp > cutoff_ms);
}

pub fn aggregate_stats(events: &[Event], now_ms: u64) -> Vec<Stats> {
    let cutoff_ms = now_ms.saturating_sub(AGGREGATION_WINDOW_MS);

    let mut map: HashMap<(String, usize), Vec<f64>> = HashMap::new();
    for event in events.iter().filter(|event| event.timestamp > cutoff_ms) {
        for (sensor_idx, &value) in event.values.iter().enumerate() {
            map.entry((event.station.clone(), sensor_idx))
                .or_default()
                .push(value);
        }
    }

    let mut aggregated = Vec::new();
    for ((station, sensor), values) in map {
        let clean_values: Vec<f64> = values.into_iter().filter(|value| value.is_finite()).collect();

        if clean_values.is_empty() {
            tracing::warn!(%station, sensor, "skipping stats aggregation: no finite values");
            continue;
        }

        let count = clean_values.len();
        let min = clean_values.iter().copied().fold(f64::INFINITY, f64::min);
        let max = clean_values
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        let mean = clean_values.iter().sum::<f64>() / count as f64;

        aggregated.push(Stats {
            station,
            sensor,
            mean,
            min,
            max,
            count,
            timestamp: now_ms,
        });
    }

    aggregated
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk_event(station: &str, timestamp: u64, values: Vec<f64>) -> Event {
        Event {
            timestamp,
            values,
            station: station.to_string(),
        }
    }

    #[test]
    fn aggregate_stats_groups_by_station_and_sensor() {
        let now = 1_000;
        let events = vec![
            mk_event("A", 900, vec![1.0, 2.0]),
            mk_event("A", 950, vec![3.0, 4.0]),
            mk_event("B", 980, vec![10.0]),
        ];

        let mut out = aggregate_stats(&events, now);
        out.sort_by(|a, b| a.station.cmp(&b.station).then(a.sensor.cmp(&b.sensor)));

        assert_eq!(out.len(), 3);
        assert_eq!(out[0].station, "A");
        assert_eq!(out[0].sensor, 0);
        assert_eq!(out[0].count, 2);
        assert!((out[0].mean - 2.0).abs() < 1e-9);

        assert_eq!(out[1].station, "A");
        assert_eq!(out[1].sensor, 1);
        assert_eq!(out[1].count, 2);
        assert!((out[1].mean - 3.0).abs() < 1e-9);

        assert_eq!(out[2].station, "B");
        assert_eq!(out[2].sensor, 0);
        assert_eq!(out[2].count, 1);
        assert!((out[2].mean - 10.0).abs() < 1e-9);
    }

    #[test]
    fn aggregate_stats_drops_old_events_outside_window() {
        let now = 10_000;
        let old_ts = now - AGGREGATION_WINDOW_MS - 1;
        let recent_ts = now - 1;
        let events = vec![mk_event("A", old_ts, vec![100.0]), mk_event("A", recent_ts, vec![2.0])];

        let out = aggregate_stats(&events, now);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].count, 1);
        assert!((out[0].mean - 2.0).abs() < 1e-9);
    }

    #[test]
    fn prune_expired_bounds_buffer_to_window() {
        let now = 10_000;
        let mut events: Vec<Event> = (0..100).map(|i| mk_event("A", now - 1_000 + i * 10, vec![1.0])).collect();
        prune_expired(&mut events, now);
        assert!(events.iter().all(|e| e.timestamp > now - AGGREGATION_WINDOW_MS));
        assert_eq!(events.len(), (AGGREGATION_WINDOW_MS / 10 - 1) as usize);
    }

    #[test]
    fn aggregate_stats_filters_non_finite_values() {
        let now = 1_000;
        let events = vec![mk_event("A", 950, vec![f64::NAN, f64::INFINITY, 5.0])];

        let out = aggregate_stats(&events, now);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].count, 1);
        assert!((out[0].mean - 5.0).abs() < 1e-9);
    }
}
