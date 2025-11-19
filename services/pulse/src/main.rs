mod simulator;

use crate::simulator::IoTSensorSimulator;
use async_nats::ConnectOptions;
use bytes::Bytes;
use tokio::time::{Duration};



#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let nats_url = "nats://nats:4222";
    let nc = ConnectOptions::new().connect(nats_url).await?;

    println!("Pulse connected to NATS at {}", nats_url);

    // Initialize environment state
    let mut simulator = IoTSensorSimulator::new(4);
    let nc_pub = nc.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_millis(5));
        loop {
            interval.tick().await;

            let sample = simulator.step();

            // Convert sample to event structure
            let event = serde_json::json!({
               "timestamp": chrono::Utc::now().timestamp_millis() as u64,
                "values": sample,
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