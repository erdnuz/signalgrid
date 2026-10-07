use anyhow::ensure;
use chrono::Utc;
use metrics::{counter, gauge};
use pulse::simulator::IoTSensorSimulator;
use signalgrid_core::config::{connect_nats, env_or};
use signalgrid_core::telemetry::{init_metrics, init_tracing};
use signalgrid_core::{shutdown_signal, subjects, RawEvent};
use tokio::time::Duration;
use tracing::{error, info};

struct Station {
    name: String,
    subject: String,
    simulator: IoTSensorSimulator,
}

fn station_name(index: usize) -> String {
    format!("Station{}", (b'A' + index as u8) as char)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing("pulse");
    init_metrics()?;

    let n_stations: usize = env_or("N_STATIONS", 2)?;
    let interval_ms: u64 = env_or("PUBLISH_INTERVAL_MS", 100)?;
    let seed: Option<u64> = std::env::var("SEED").ok().map(|s| s.parse()).transpose()?;
    ensure!(
        (1..=26).contains(&n_stations),
        "N_STATIONS must be between 1 and 26"
    );
    ensure!(interval_ms > 0, "PUBLISH_INTERVAL_MS must be positive");

    let mut stations: Vec<Station> = (0..n_stations)
        .map(|i| {
            let name = station_name(i);
            let simulator = match seed {
                Some(seed) => IoTSensorSimulator::with_seed(seed.wrapping_add(i as u64)),
                None => IoTSensorSimulator::new(),
            };
            Station {
                subject: subjects::raw(&name),
                name,
                simulator,
            }
        })
        .collect();

    let nc = connect_nats("pulse").await?;
    info!(
        event = "publishing",
        n_stations,
        interval_ms,
        seeded = seed.is_some()
    );

    // Sample just after each wall-clock grid point (multiples of the
    // interval). Forge's windows sit on the same grid, so the sample taken
    // right after a window boundary is what tells forge the previous window
    // is complete; with free-running ticks it would arrive up to one interval
    // late, adding that much end-to-end latency.
    //
    // Samples carry their *actual* timestamp: snapping it to the grid would
    // stamp late ticks in the future (and show up downstream as negative
    // latency). Each deadline is recomputed from the wall clock, so drift
    // between the monotonic and wall clocks cannot accumulate.
    let step = interval_ms as i64;
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        let now_ms = Utc::now().timestamp_millis();
        let wait = next_tick_after(now_ms, step) - now_ms;
        tokio::select! {
            _ = &mut shutdown => break,
            _ = tokio::time::sleep(Duration::from_millis(wait as u64)) => {}
        }

        let timestamp = Utc::now().timestamp_millis();
        for station in &mut stations {
            let values = station.simulator.step();
            let regime = station.simulator.regime_idx as u8;
            gauge!("pulse_regime", "station" => station.name.clone()).set(regime as f64);

            let event = RawEvent {
                station: station.name.clone(),
                timestamp,
                values,
                regime: Some(regime),
            };
            let payload = match serde_json::to_vec(&event) {
                Ok(p) => p,
                Err(e) => {
                    error!(event = "encode_failed", error = %e);
                    continue;
                }
            };
            match nc.publish(station.subject.clone(), payload.into()).await {
                Ok(()) => counter!("pulse_events_published_total").increment(1),
                Err(e) => error!(event = "publish_failed", error = %e),
            }
        }
    }

    nc.flush().await?;
    Ok(())
}

/// Margin after a grid point, so a timer that fires marginally early still
/// samples inside the new window.
const TICK_OFFSET_MS: i64 = 1;

/// Next sampling instant strictly after `now_ms`: a grid point plus the offset.
fn next_tick_after(now_ms: i64, step: i64) -> i64 {
    (now_ms - TICK_OFFSET_MS).div_euclid(step) * step + step + TICK_OFFSET_MS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ticks_land_just_after_grid_points() {
        assert_eq!(next_tick_after(1_000, 100), 1_001);
        assert_eq!(next_tick_after(1_001, 100), 1_101);
        assert_eq!(next_tick_after(1_050, 100), 1_101);
        assert_eq!(next_tick_after(1_100, 100), 1_101);
    }

    #[test]
    fn station_names_are_valid_subject_tokens() {
        for i in 0..26 {
            assert!(subjects::is_valid_token(&station_name(i)));
        }
        assert_eq!(station_name(0), "StationA");
    }
}
