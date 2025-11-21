mod simulator;

use crate::simulator::IoTSensorSimulator;
use async_nats::ConnectOptions;
use bytes::Bytes;
use tokio::time::{Duration};
use chrono::Utc;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let nats_url = "nats://nats:4222";
    let nc = ConnectOptions::new().connect(nats_url).await?;

    println!("Pulse connected to NATS at {}", nats_url);

    // Initialize simulator with 4 channels and 3 regimes
    let mut simulator = IoTSensorSimulator::new();
    let nc_pub = nc.clone();

    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_millis(100)); // realistic interval
        loop {
            interval.tick().await;

            let sample = simulator.step();

            // Convert sample to event structure, include current regime
            let event = serde_json::json!({
                "timestamp": Utc::now().timestamp_millis() as u64,
                "values": sample,
                "regime": simulator.regime_idx,
                "station":"StationA"
            });

            let payload: Bytes = serde_json::to_vec(&event).unwrap().into();
            if let Err(e) = nc_pub.publish("events", payload).await {
                eprintln!("Failed to publish to NATS: {:?}", e);
            }
        }
    });

    tokio::signal::ctrl_c().await?;
    println!("Shutting down...");

    Ok(())
}
