use async_nats::ConnectOptions;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::time::{self, Duration};
use warp::Filter;
use chrono::{Utc, DateTime};

#[derive(Debug, Serialize, Deserialize, Clone, Hash, PartialEq, Eq)]
enum Sensor {
    Temperature,
    Pressure,
    Humidity,
    Wind,
    Aqi,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Event {
    id: String,
    station: String,
    sensor: Sensor,
    timestamp: u64, // milliseconds since epoch
    value: f64,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Stats {
    station: String,
    sensor: Sensor,
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

    let last_event: Arc<tokio::sync::Mutex<Option<Event>>> = Arc::new(tokio::sync::Mutex::new(None));
    let stats_history: Arc<tokio::sync::Mutex<Vec<Stats>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));

    let mut sub = client.subscribe("events").await?;
    let last_event_clone = last_event.clone();
    let stats_history_clone = stats_history.clone();
    let client_clone = client.clone();


    // ----- NATS listener -----
    tokio::spawn(async move {
        let mut buffer: Vec<Event> = Vec::new();
        let mut sleep = Box::pin(tokio::time::sleep(Duration::from_secs(5)));

        loop {
            tokio::select! {
                maybe_msg = sub.next() => {
                    if let Some(msg) = maybe_msg {
                        if let Ok(event) = serde_json::from_slice::<Event>(&msg.payload) {
                            let mut guard = last_event_clone.lock().await;
                            *guard = Some(event.clone());
                            buffer.push(event);
                        }
                    }
                }

                // Sleep should only be awaited once per cycle
                _ = &mut sleep => {
                    // Do aggregation...
                    let now = Utc::now().timestamp_millis() as u64;
                    let cutoff = now - 10_000;
                    buffer.retain(|e| e.timestamp >= cutoff);

                    // Aggregate by (station, sensor)
                    let mut map: HashMap<(String, Sensor), Vec<f64>> = HashMap::new();
                    for e in &buffer {
                        map.entry((e.station.clone(), e.sensor.clone()))
                            .or_default()
                            .push(e.value);
                    }

                    let mut stats_to_publish = Vec::new();
                    for ((station, sensor), values) in map {
                        let count = values.len();
                        let min = *values.iter().min_by(|a, b| a.partial_cmp(b).unwrap()).unwrap();
                        let max = *values.iter().max_by(|a, b| a.partial_cmp(b).unwrap()).unwrap();
                        let mean = values.iter().sum::<f64>() / count as f64;

                        stats_to_publish.push(Stats {
                            station,
                            sensor,
                            mean,
                            min,
                            max,
                            count,
                            timestamp: Utc::now().timestamp_millis() as u64,
                        });
                    }

                    {
                        let mut guard = stats_history_clone.lock().await;
                        guard.extend(stats_to_publish.clone());
                    }

                    for stat in stats_to_publish {
                        let payload: Bytes = serde_json::to_vec(&stat).unwrap().into();
                        if let Err(e) = client_clone.publish("stats", payload).await {
                            eprintln!("Failed to publish stats: {:?}", e);
                        }
                    }

                    // Reset the sleep future
                    sleep = Box::pin(time::sleep(Duration::from_secs(5)));
                }
            }
        }

    });

    // ----- HTTP server -----
    let last_event_filter = warp::any().map({
        let last_event = last_event.clone();
        move || last_event.clone()
    });
    let stats_filter = warp::any().map({
        let stats_history = stats_history.clone();
        move || stats_history.clone()
    });

    let route = warp::path("forge")
        .and(last_event_filter)
        .and(stats_filter)
        .and_then(
            |last_event: Arc<tokio::sync::Mutex<Option<Event>>>,
             stats_history: Arc<tokio::sync::Mutex<Vec<Stats>>>| async move
            {
                let e = last_event.lock().await;
                let stats = stats_history.lock().await;
                Ok::<_, warp::Rejection>(warp::reply::json(&(e.clone(), stats.clone())))
            },
        )
        .with(warp::cors().allow_any_origin());

    println!("HTTP server running at http://0.0.0.0:8002");
    warp::serve(route).run(([0, 0, 0, 0], 8002)).await;

    Ok(())
}
