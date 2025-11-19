use async_nats::ConnectOptions;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::collections::HashMap;
use tokio::time::{self, Duration};
use chrono::Utc;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Event {
    timestamp: u64,
    values: Vec<f64>, // array of sensor values
    station: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Stats {
    station: String,
    sensor: usize,
    mean: f64,
    min: f64,
    max: f64,
    count: usize,
    timestamp: u64,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let nats_url = "nats://nats:4222";
    let client = ConnectOptions::new().connect(nats_url).await?;
    println!("Forge connected to NATS at {}", nats_url);

    let stats_history: Arc<tokio::sync::Mutex<Vec<Stats>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let mut sub = client.subscribe("events").await?;
    let stats_history_clone = stats_history.clone();
    let client_clone = client.clone();

    tokio::spawn(async move {
        let mut buffer: Vec<Event> = Vec::new();
        let mut sleep = Box::pin(time::sleep(Duration::from_secs(5)));

        loop {
            tokio::select! {
                maybe_msg = sub.next() => {
                    if let Some(msg) = maybe_msg {
                        if let Ok(event) = serde_json::from_slice::<Event>(&msg.payload) {
                            buffer.push(event);
                        }
                    }
                }

                _ = &mut sleep => {
                    if !buffer.is_empty() {
                        let now_ms = Utc::now().timestamp_millis() as u64;
                        let cutoff_ms = now_ms.saturating_sub(2_000); // 30 seconds in ms
                        buffer.retain(|e| e.timestamp > cutoff_ms);

                        println!("Forge processing {} events", buffer.len());

                        // Aggregate stats per sensor index
                        let mut map: HashMap<(String, usize), Vec<f64>> = HashMap::new();
                        for e in &buffer {
                            for (i, &value) in e.values.iter().enumerate() {
                                map.entry((e.station.clone(), i)).or_default().push(value);
                            }
                        }

                        let mut stats_to_publish = Vec::new();
                        for ((station, sensor), values) in map {
                            let count = values.len();
                            let min = *values.iter().min_by(|a, b| a.partial_cmp(b).unwrap()).unwrap();
                            let max = *values.iter().max_by(|a, b| a.partial_cmp(b).unwrap()).unwrap();
                            let mean = values.iter().sum::<f64>() / count as f64;

                            stats_to_publish.push(Stats {
                                station: station.clone(),
                                sensor,
                                mean,
                                min,
                                max,
                                count,
                                timestamp: chrono::Utc::now().timestamp_millis() as u64,
                            });
                        }

                        // Store history
                        {
                            let mut guard = stats_history_clone.lock().await;
                            guard.extend(stats_to_publish.clone());
                        }

                        // Publish stats
                        for stat in stats_to_publish {
                            let payload: Bytes = serde_json::to_vec(&stat).unwrap().into();
                            if let Err(e) = client_clone.publish("stats", payload).await {
                                eprintln!("Failed to publish stats: {:?}", e);
                            }

                        }
                    }

                    // Reset sleep
                    sleep = Box::pin(time::sleep(Duration::from_secs(1)));
                }
            }
        }
    });

    tokio::signal::ctrl_c().await?;
    println!("Shutting down...");
    Ok(())
}
