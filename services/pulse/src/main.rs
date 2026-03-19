use pulse::simulator::IoTSensorSimulator;
use async_nats::ConnectOptions;
use bytes::Bytes;
use std::env;
use anyhow::Context;
use tokio::sync::watch;
use tokio::time::{Duration};
use chrono::Utc;
use tracing::{info, error};
use tracing_subscriber;

const PUBLISH_INTERVAL_MS: u64 = 100;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;
    let nc = ConnectOptions::new().connect(&nats_url).await?;

    info!(service = "pulse", event = "nats_connected", %nats_url);

    // Initialize simulator with 4 channels and 3 regimes
    let mut simulator = IoTSensorSimulator::new();
    let nc_pub = nc.clone();
    let (shutdown_tx, mut shutdown_rx) = watch::channel(false);

    let publish_task = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_millis(PUBLISH_INTERVAL_MS));
        loop {
            tokio::select! {
                changed = shutdown_rx.changed() => {
                    if changed.is_ok() && *shutdown_rx.borrow() {
                        info!(service = "pulse", event = "stopping_publisher");
                        break;
                    }
                }
                _ = interval.tick() => {}
            }

            let sample = simulator.step();

            // Convert sample to event structure, include current regime
            let event = serde_json::json!({
                "timestamp": Utc::now().timestamp_millis() as u64,
                "values": sample,
                "regime": simulator.regime_idx,
                "station":"StationA"
            });

            match serde_json::to_vec(&event) {
                Ok(encoded) => {
                    let payload: Bytes = encoded.into();
                    if let Err(e) = nc_pub.publish("events", payload).await {
                        error!(service = "pulse", event = "publish_failed", error = %e);
                    }
                }
                Err(e) => {
                    error!(service = "pulse", event = "encode_failed", error = %e);
                }
            }
        }
    });

    tokio::signal::ctrl_c().await?;
    info!(service = "pulse", event = "signal_received", signal = "ctrl_c");
    let _ = shutdown_tx.send(true);

    if let Err(e) = publish_task.await {
        error!(service = "pulse", event = "task_join_error", error = %e);
    }

    Ok(())
}
