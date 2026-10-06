//! Wire format for messages produced by the Rust services. The JSON golden
//! files in `contracts/` are checked against these types here and against the
//! Python models in the Python test suites, so a breaking change fails CI on
//! both sides.

use serde::{Deserialize, Serialize};

/// One multi-channel sample from a station, published by `pulse`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RawEvent {
    pub station: String,
    /// Event time, Unix epoch milliseconds.
    pub timestamp: i64,
    pub values: Vec<f64>,
    /// Ground-truth simulator regime; absent for real sensors.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regime: Option<u8>,
}

/// Per-sensor summary of one tumbling event-time window, published by `forge`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Stats {
    pub station: String,
    pub sensor: u32,
    /// Window start, Unix epoch milliseconds (inclusive).
    pub timestamp: i64,
    pub window_ms: i64,
    pub mean: f64,
    pub min: f64,
    pub max: f64,
    pub count: u64,
    /// Most frequent ground-truth regime within the window, if known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regime: Option<u8>,
}

impl Stats {
    /// Deterministic identity of a window, used for JetStream de-duplication
    /// (`Nats-Msg-Id`); the database enforces the same identity as its key.
    pub fn message_id(&self) -> String {
        format!("{}.{}.{}", self.station, self.sensor, self.timestamp)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn contract(name: &str) -> serde_json::Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../contracts")
            .join(name);
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
        serde_json::from_str(&text).expect("valid JSON")
    }

    fn sample_raw() -> RawEvent {
        RawEvent {
            station: "StationA".into(),
            timestamp: 1_700_000_000_100,
            values: vec![0.12, -0.5, 1.75, 2.0],
            regime: Some(1),
        }
    }

    fn sample_stats() -> Stats {
        Stats {
            station: "StationA".into(),
            sensor: 2,
            timestamp: 1_700_000_000_000,
            window_ms: 500,
            mean: 1.5,
            min: 1.25,
            max: 1.75,
            count: 5,
            regime: Some(1),
        }
    }

    #[test]
    fn raw_event_matches_contract() {
        assert_eq!(
            serde_json::to_value(sample_raw()).unwrap(),
            contract("raw_event.json")
        );
        let parsed: RawEvent = serde_json::from_value(contract("raw_event.json")).unwrap();
        assert_eq!(parsed, sample_raw());
    }

    #[test]
    fn stats_matches_contract() {
        assert_eq!(
            serde_json::to_value(sample_stats()).unwrap(),
            contract("stats.json")
        );
        let parsed: Stats = serde_json::from_value(contract("stats.json")).unwrap();
        assert_eq!(parsed, sample_stats());
    }

    #[test]
    fn regime_is_optional_on_the_wire() {
        let raw = RawEvent {
            regime: None,
            ..sample_raw()
        };
        let json = serde_json::to_value(&raw).unwrap();
        assert!(json.get("regime").is_none());
        assert_eq!(serde_json::from_value::<RawEvent>(json).unwrap(), raw);
    }

    #[test]
    fn message_id_identifies_the_window() {
        assert_eq!(sample_stats().message_id(), "StationA.2.1700000000000");
    }
}
