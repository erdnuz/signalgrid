use async_nats::ConnectOptions;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::time::{self, Duration};
use warp::Filter;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Event {
    id: String,
    message: String,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    println!("Forge: Subscribing to NATS");

    let nats_url = "nats://nats:4222".to_string();

    let client = match ConnectOptions::new().connect(&nats_url).await {
        Ok(c) => {
            println!("Connected to NATS at {}", nats_url);
            c
        }
        Err(e) => {
            eprintln!("Failed to connect to NATS at {}: {:?}", nats_url, e);
            return Err(e.into()); // returns an anyhow::Error
        }
    };



    println!("Forge connected to NATS at {}", nats_url);

    // Shared last processed event
    let last_event: Arc<tokio::sync::Mutex<Option<Event>>> =
        Arc::new(tokio::sync::Mutex::new(None));

    // Subscribe to events
    let sub = client.subscribe("events").await?;


    // Convert Subscriber → Stream
    let mut messages = sub;

    // ---- NATS listener task ----
    let last_event_clone = last_event.clone();
    let client_clone = client.clone(); // need a clone to publish
    let nats_handle = tokio::spawn(async move {
        while let Some(msg) = messages.next().await {
            let payload: Bytes = msg.payload;

            match serde_json::from_slice::<Event>(&payload) {
                Ok(mut event) => {
                    // --- Transform the event ---
                    event.message = format!("{}. This message has been processed", event.message);

                    println!("Forge processed: {:?}", event);

                    // Update last_event for HTTP endpoint
                    let mut guard = last_event_clone.lock().await;
                    *guard = Some(event.clone());

                    // Publish the enriched event to a new NATS subject
                    let enriched_payload: Bytes = serde_json::to_vec(&event)
                        .expect("Failed to serialize enriched event")
                        .into();

                    if let Err(e) = client_clone.publish("enriched_events", enriched_payload).await {
                        eprintln!("Failed to publish enriched event: {:?}", e);
                    }
                }
                Err(e) => eprintln!("Forge: Failed to parse event: {:?}", e),
            }
        }

        // If subscription ends, keep the task alive
        loop {
            eprintln!("Forge: Subscription ended, retrying in 5s...");
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    });


    // ---- Warp HTTP server ----
    let last_event_filter = warp::any().map({
        let last_event = last_event.clone();
        move || last_event.clone()
    });

    let route = warp::path("forge")
        .and(last_event_filter)
        .and_then(|last_event: Arc<tokio::sync::Mutex<Option<Event>>>| async move {
            let e = last_event.lock().await;
            Ok::<_, warp::Rejection>(warp::reply::json(&*e))
        })
        .with(warp::cors().allow_any_origin());

    println!("HTTP server running at http://0.0.0.0:8002");
    warp::serve(route).run(([0, 0, 0, 0], 8002)).await;


    // Keep main alive by awaiting both tasks
    nats_handle.await?;

    Ok(())
}
