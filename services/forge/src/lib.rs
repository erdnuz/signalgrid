//! Event-time tumbling-window aggregation.
//!
//! Windows are aligned to multiples of `window_ms` on the event timestamp, so
//! every sample belongs to exactly one window regardless of when it arrives.
//! A window is closed once the watermark (max event time seen minus the
//! allowed lateness) passes its end. Events for an already-closed window are
//! reported as late and dropped. Each open window holds only O(1) running
//! accumulators per (station, sensor), so memory is bounded by the number of
//! open windows rather than by the event rate.

use signalgrid_core::{RawEvent, Stats};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy)]
struct Accumulator {
    count: u64,
    sum: f64,
    min: f64,
    max: f64,
}

impl Accumulator {
    fn new(value: f64) -> Self {
        Self {
            count: 1,
            sum: value,
            min: value,
            max: value,
        }
    }

    fn push(&mut self, value: f64) {
        self.count += 1;
        self.sum += value;
        self.min = self.min.min(value);
        self.max = self.max.max(value);
    }
}

#[derive(Debug, Default)]
struct Window {
    sensors: BTreeMap<(String, u32), Accumulator>,
    /// Ground-truth regime counts per station.
    regimes: BTreeMap<String, BTreeMap<u8, u32>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ingest {
    /// Accepted; `dropped_values` non-finite readings were ignored.
    Accepted { dropped_values: usize },
    /// The event's window was already closed.
    Late,
}

#[derive(Debug)]
pub struct WindowAggregator {
    window_ms: i64,
    allowed_lateness_ms: i64,
    open: BTreeMap<i64, Window>,
    max_event_ts: Option<i64>,
    /// Windows starting before this are closed; events for them are late.
    closed_before: i64,
}

impl WindowAggregator {
    pub fn new(window_ms: i64, allowed_lateness_ms: i64) -> Self {
        assert!(window_ms > 0, "window_ms must be positive");
        assert!(allowed_lateness_ms >= 0, "allowed_lateness_ms must be >= 0");
        Self {
            window_ms,
            allowed_lateness_ms,
            open: BTreeMap::new(),
            max_event_ts: None,
            closed_before: i64::MIN,
        }
    }

    pub fn window_ms(&self) -> i64 {
        self.window_ms
    }

    pub fn window_start(&self, ts: i64) -> i64 {
        ts - ts.rem_euclid(self.window_ms)
    }

    pub fn open_windows(&self) -> usize {
        self.open.len()
    }

    pub fn watermark(&self) -> Option<i64> {
        self.max_event_ts
            .map(|ts| ts.saturating_sub(self.allowed_lateness_ms))
    }

    pub fn ingest(&mut self, event: &RawEvent) -> Ingest {
        let start = self.window_start(event.timestamp);
        if start < self.closed_before {
            return Ingest::Late;
        }

        let window = self.open.entry(start).or_default();
        let mut dropped_values = 0;
        for (sensor, &value) in event.values.iter().enumerate() {
            if !value.is_finite() {
                dropped_values += 1;
                continue;
            }
            window
                .sensors
                .entry((event.station.clone(), sensor as u32))
                .and_modify(|acc| acc.push(value))
                .or_insert_with(|| Accumulator::new(value));
        }
        if let Some(regime) = event.regime {
            *window
                .regimes
                .entry(event.station.clone())
                .or_default()
                .entry(regime)
                .or_default() += 1;
        }

        self.max_event_ts = Some(
            self.max_event_ts
                .map_or(event.timestamp, |m| m.max(event.timestamp)),
        );
        Ingest::Accepted { dropped_values }
    }

    /// Closes and returns every window whose end is at or before the watermark.
    pub fn close_ready(&mut self) -> Vec<Stats> {
        match self.watermark() {
            // A window [s, s + w) is complete when s + w <= watermark.
            Some(wm) => {
                self.close_starting_before(wm.saturating_sub(self.window_ms).saturating_add(1))
            }
            None => Vec::new(),
        }
    }

    /// Closes every open window (used on shutdown or when the input goes idle).
    pub fn close_all(&mut self) -> Vec<Stats> {
        match self.open.keys().next_back() {
            Some(&last) => self.close_starting_before(last + 1),
            None => Vec::new(),
        }
    }

    fn close_starting_before(&mut self, bound: i64) -> Vec<Stats> {
        let still_open = self.open.split_off(&bound);
        let ready = std::mem::replace(&mut self.open, still_open);
        self.closed_before = self.closed_before.max(bound);

        let mut out = Vec::new();
        for (start, window) in ready {
            for ((station, sensor), acc) in window.sensors {
                let regime = window.regimes.get(&station).and_then(majority);
                out.push(Stats {
                    station,
                    sensor,
                    timestamp: start,
                    window_ms: self.window_ms,
                    mean: acc.sum / acc.count as f64,
                    min: acc.min,
                    max: acc.max,
                    count: acc.count,
                    regime,
                });
            }
        }
        out
    }
}

/// Most frequent regime; ties resolve to the lowest regime id.
fn majority(counts: &BTreeMap<u8, u32>) -> Option<u8> {
    counts
        .iter()
        .max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0)))
        .map(|(&regime, _)| regime)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(station: &str, ts: i64, values: Vec<f64>, regime: Option<u8>) -> RawEvent {
        RawEvent {
            station: station.to_string(),
            timestamp: ts,
            values,
            regime,
        }
    }

    #[test]
    fn groups_by_window_station_and_sensor() {
        let mut agg = WindowAggregator::new(500, 0);
        agg.ingest(&ev("A", 1_000, vec![1.0, 2.0], None));
        agg.ingest(&ev("A", 1_400, vec![3.0, 4.0], None));
        agg.ingest(&ev("B", 1_200, vec![10.0], None));

        let out = agg.close_all();
        assert_eq!(out.len(), 3);
        let a0 = out
            .iter()
            .find(|s| s.station == "A" && s.sensor == 0)
            .unwrap();
        assert_eq!(a0.timestamp, 1_000);
        assert_eq!(a0.count, 2);
        assert!((a0.mean - 2.0).abs() < 1e-12);
        assert_eq!((a0.min, a0.max), (1.0, 3.0));
    }

    #[test]
    fn windows_close_only_after_watermark_passes_their_end() {
        let mut agg = WindowAggregator::new(500, 100);
        agg.ingest(&ev("A", 1_000, vec![1.0], None));
        agg.ingest(&ev("A", 1_550, vec![1.0], None));
        // watermark = 1450 < window end 1500
        assert!(agg.close_ready().is_empty());

        agg.ingest(&ev("A", 1_600, vec![1.0], None));
        // watermark = 1500 -> [1000, 1500) closes, [1500, 2000) stays open
        let out = agg.close_ready();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].timestamp, 1_000);
        assert_eq!(agg.open_windows(), 1);
    }

    #[test]
    fn events_for_closed_windows_are_late() {
        let mut agg = WindowAggregator::new(500, 0);
        agg.ingest(&ev("A", 2_100, vec![1.0], None));
        // watermark 2100: [1500, 2000) is complete even though it saw no data
        assert!(agg.close_ready().is_empty());
        assert_eq!(agg.ingest(&ev("A", 1_900, vec![9.0], None)), Ingest::Late);
        assert!(matches!(
            agg.ingest(&ev("A", 2_200, vec![1.0], None)),
            Ingest::Accepted { .. }
        ));
    }

    #[test]
    fn filters_non_finite_values() {
        let mut agg = WindowAggregator::new(500, 0);
        let outcome = agg.ingest(&ev("A", 1_000, vec![f64::NAN, f64::INFINITY, 5.0], None));
        assert_eq!(outcome, Ingest::Accepted { dropped_values: 2 });
        let out = agg.close_all();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].sensor, 2);
    }

    #[test]
    fn reports_majority_regime_per_station() {
        let mut agg = WindowAggregator::new(500, 0);
        for regime in [2, 2, 1] {
            agg.ingest(&ev("A", 1_000, vec![0.0], Some(regime)));
        }
        assert_eq!(agg.close_all()[0].regime, Some(2));
    }

    #[test]
    fn negative_timestamps_align_to_window_floor() {
        let agg = WindowAggregator::new(500, 0);
        assert_eq!(agg.window_start(-1), -500);
        assert_eq!(agg.window_start(0), 0);
        assert_eq!(agg.window_start(499), 0);
    }
}
