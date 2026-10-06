use anyhow::ensure;
use chrono::Utc;
use metrics::{counter, gauge};
use pulse::simulator::IoTSensorSimulator;
use signalgrid_core::config::{connect_nats, env_or};
use signalgrid_core::telemetry::{init_metrics, init_tracing};
use signalgrid_core::{shutdown_signal, subjects, RawEvent};
use tokio::time::{Duration, MissedTickBehavior};
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

    let mut interval = tokio::time::interval(Duration::from_millis(interval_ms));
    interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        tokio::select! {
            _ = &mut shutdown => break,
            _ = interval.tick() => {}
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn station_names_are_valid_subject_tokens() {
        for i in 0..26 {
            assert!(subjects::is_valid_token(&station_name(i)));
        }
        assert_eq!(station_name(0), "StationA");
    }
}
