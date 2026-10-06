use anyhow::Context;
use async_nats::ConnectOptions;
use bytes::Bytes;
use chrono::Utc;
use pulse::simulator::IoTSensorSimulator;
use std::env;
use tokio::time::Duration;
use tracing::{error, info};

const PUBLISH_INTERVAL_MS: u64 = 100;

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler");
        tokio::select! {
            _ = ctrl_c => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = ctrl_c.await;
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;
    let nc = ConnectOptions::new()
        .retry_on_initial_connect()
        .connect(&nats_url)
        .await?;
    info!(service = "pulse", event = "nats_connected", %nats_url);

    let mut simulator = IoTSensorSimulator::new();
    let mut interval = tokio::time::interval(Duration::from_millis(PUBLISH_INTERVAL_MS));
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        tokio::select! {
            _ = &mut shutdown => {
                info!(service = "pulse", event = "signal_received");
                break;
            }
            _ = interval.tick() => {}
        }

        let sample = simulator.step();
        let event = serde_json::json!({
            "timestamp": Utc::now().timestamp_millis() as u64,
            "values": sample,
            "regime": simulator.regime_idx,
            "station": "StationA"
        });

        match serde_json::to_vec(&event) {
            Ok(encoded) => {
                let payload: Bytes = encoded.into();
                if let Err(e) = nc.publish("events", payload).await {
                    error!(service = "pulse", event = "publish_failed", error = %e);
                }
            }
            Err(e) => error!(service = "pulse", event = "encode_failed", error = %e),
        }
    }

    nc.flush().await?;
    Ok(())
}
