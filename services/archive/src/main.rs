use async_nats::ConnectOptions;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
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
    println!("Archive: Starting up");

    // -------------------------
    // 1. Connect to NATS
    // -------------------------
    let nats_url = "nats://nats:4222".to_string();
    let client = match ConnectOptions::new().connect(&nats_url).await {
        Ok(c) => {
            println!("Archive connected to NATS at {}", nats_url);
            c
        }
        Err(e) => {
            eprintln!("Archive: Failed to connect to NATS at {}: {:?}", nats_url, e);
            return Err(e.into());
        }
    };

    // -------------------------
    // 2. Connect to Database
    // -------------------------
    println!("Archive: Connecting to database");
    let db_url = "postgres://signalgrid:signalgrid@postgres:5432/signalgrid".to_string();
    let pool = PgPoolOptions::new().max_connections(5).connect(&db_url).await?;

    // -------------------------
    // 3. Shared last archived event
    // -------------------------
    let last_event: Arc<tokio::sync::Mutex<Option<Event>>> = Arc::new(tokio::sync::Mutex::new(None));

    // -------------------------
    // 4. HTTP server
    // -------------------------
    let last_event_filter = warp::any().map({
        let last_event = last_event.clone();
        move || last_event.clone()
    });

    let fetch_route = warp::path("archive")
        .and(last_event_filter)
        .and_then(|last_event: Arc<tokio::sync::Mutex<Option<Event>>>| async move {
            let event = last_event.lock().await;
            Ok::<_, warp::Rejection>(warp::reply::json(&*event))
        })
        .with(warp::cors().allow_any_origin());

    

    // -------------------------
    // 5. NATS listener
    // -------------------------
    let sub = client.subscribe("enriched_events").await?;
    let mut messages = sub;

    let last_event_clone = last_event.clone();
    let pool_clone = pool.clone();
    let nats_handle = tokio::spawn(async move {
        while let Some(msg) = messages.next().await {
            let payload: Bytes = msg.payload;

            match serde_json::from_slice::<Event>(&payload) {
                Ok(event) => {
                    println!("Archive processed event: {:?}", event);

                    // Insert into database
                    if let Err(e) = sqlx::query(
                        "INSERT INTO events (id, message) VALUES ($1, $2)"
                    )
                    .bind(&event.id)
                    .bind(&event.message)
                    .execute(&pool_clone)
                    .await
                    {
                        eprintln!("Archive: Failed to insert event into DB: {:?}", e);
                    }


                    // Update last archived event
                    let mut guard = last_event_clone.lock().await;
                    *guard = Some(event);
                }
                Err(e) => eprintln!("Archive: Failed to parse event: {:?}", e),
            }
        }

        // Keep listener alive if subscription ends
        loop {
            eprintln!("Archive: NATS subscription ended, retrying in 5s...");
            time::sleep(Duration::from_secs(5)).await;
        }
    });

    println!("HTTP server running at http://0.0.0.0:8003");
    warp::serve(fetch_route).run(([0, 0, 0, 0], 8003)).await;


    // Keep main alive by awaiting both tasks
    nats_handle.await?;

    Ok(())
}
