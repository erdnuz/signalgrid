use forge::{Ingest, WindowAggregator};
use proptest::prelude::*;
use signalgrid_core::RawEvent;

fn value() -> impl Strategy<Value = f64> {
    prop_oneof![
        8 => -1e6f64..1e6,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
    ]
}

fn event() -> impl Strategy<Value = RawEvent> {
    (
        prop::sample::select(vec!["A", "B", "C"]),
        0i64..20_000,
        prop::collection::vec(value(), 1..5),
        prop::option::of(0u8..3),
    )
        .prop_map(|(station, timestamp, values, regime)| RawEvent {
            station: station.to_string(),
            timestamp,
            values,
            regime,
        })
}

proptest! {
    /// Every finite value from an accepted event is counted exactly once, and
    /// every emitted window is internally consistent.
    #[test]
    fn aggregation_conserves_values_and_is_consistent(
        events in prop::collection::vec(event(), 0..300),
        window_ms in 50i64..2_000,
        lateness in 0i64..1_000,
        close_every in 1usize..20,
    ) {
        let mut agg = WindowAggregator::new(window_ms, lateness);
        let mut accepted_finite = 0u64;
        let mut emitted = Vec::new();

        for (i, ev) in events.iter().enumerate() {
            if let Ingest::Accepted { dropped_values } = agg.ingest(ev) {
                accepted_finite += (ev.values.len() - dropped_values) as u64;
            }
            if i % close_every == 0 {
                emitted.extend(agg.close_ready());
            }
        }
        emitted.extend(agg.close_all());

        prop_assert_eq!(emitted.iter().map(|s| s.count).sum::<u64>(), accepted_finite);
        prop_assert_eq!(agg.open_windows(), 0);

        let mut seen = std::collections::HashSet::new();
        for s in &emitted {
            prop_assert!(s.count > 0);
            prop_assert!(s.min <= s.mean + 1e-6 && s.mean <= s.max + 1e-6, "{:?}", s);
            prop_assert_eq!(s.timestamp.rem_euclid(window_ms), 0);
            prop_assert_eq!(s.window_ms, window_ms);
            // A window is emitted at most once per (station, sensor).
            prop_assert!(seen.insert(s.message_id()), "duplicate window {}", s.message_id());
        }
    }
}
