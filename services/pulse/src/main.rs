use async_nats::ConnectOptions;
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::time::{self, Duration};
use uuid::Uuid;
use warp::Filter;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Event {
    id: String,
    message: String,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // -------------------------
    // 1. Connect to NATS
    // -------------------------
    let nats_url = "nats://nats:4222".to_string();
    let nc = ConnectOptions::new()
        .connect(&nats_url)
        .await
        .map_err(|e| {
            eprintln!("Failed to connect to NATS at {}: {:?}", nats_url, e);
            e
        })?;

    println!("Pulse connected to NATS at {}", nats_url);

    // -------------------------
    // 2. Shared state for last event
    // -------------------------
    let last_event: Arc<tokio::sync::Mutex<Option<Event>>> = Arc::new(tokio::sync::Mutex::new(None));

    // -------------------------
    // 3. HTTP server for last event
    // -------------------------
    let last_event_filter = warp::any().map({
        let last_event = last_event.clone();
        move || last_event.clone()
    });

    let fetch_route = warp::path("events")
        .and(last_event_filter)
        .and_then(|last_event: Arc<tokio::sync::Mutex<Option<Event>>>| async move {
            let event = last_event.lock().await;
            Ok::<_, warp::Rejection>(warp::reply::json(&*event))
        })
        .with(warp::cors().allow_any_origin());


    // Spawn NATS publisher loop
    let last_event_clone = last_event.clone();
    let nats_task = tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(1));
        loop {
            interval.tick().await;

            let event = Event {
                id: Uuid::new_v4().to_string(),
                message: "Hello from Pulse!".to_string(),
            };

            let payload: Bytes = match serde_json::to_vec(&event) {
                Ok(vec) => vec.into(),
                Err(e) => {
                    eprintln!("Failed to serialize event: {:?}", e);
                    continue;
                }
            };

            if let Err(e) = nc.publish("events", payload).await {
                eprintln!("Failed to publish event to NATS: {:?}", e);
                continue;
            }

            println!("Published event to NATS: {:?}", event);

            let mut guard = last_event_clone.lock().await;
            *guard = Some(event);
        }
    });

    // Run Warp HTTP server **directly**, not inside spawn
    println!("HTTP server running at http://0.0.0.0:8001");
    warp::serve(fetch_route).run(([0, 0, 0, 0], 8001)).await;

    // Optionally await NATS task if needed (it runs indefinitely)
    nats_task.await?;


    Ok(())
}
