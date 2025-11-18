use async_nats::ConnectOptions;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use std::sync::Arc;
use tokio::time::{self, Duration};
use sqlx::FromRow;
use warp::Filter;
use std::convert::Infallible;
use std::collections::HashMap;
use uuid::Uuid;

// Helper to pass DB pool into warp filters
fn with_db(
    pool: sqlx::PgPool,
) -> impl Filter<Extract = (sqlx::PgPool,), Error = Infallible> + Clone {
    warp::any().map(move || pool.clone())
}


#[derive(Debug, Serialize, Deserialize, Clone, FromRow)]
struct Stats {
    station: String,
    sensor: String,
    mean: f64,
    min: f64,
    max: f64,
    count: i64,
    timestamp: i64,
}

// Query historical events
async fn query_events(
    pool: &sqlx::PgPool,
    sensor: Option<&str>,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
) -> anyhow::Result<Vec<Stats>> {
    let mut conditions = Vec::new();
    if sensor.is_some() {
        conditions.push(format!("sensor = '{}'", sensor.unwrap()));
    }
    if let Some(start) = start_ts {
        conditions.push(format!("timestamp >= {}", start));
    }
    if let Some(end) = end_ts {
        conditions.push(format!("timestamp <= {}", end));
    }

    let where_clause = if conditions.is_empty() {
        "".to_string()
    } else {
        format!("WHERE {}", conditions.join(" AND "))
    };

    let query_str = format!("SELECT station, sensor, timestamp, mean, min, max, count FROM stats {}", where_clause);

    let events = sqlx::query_as::<_, Stats>(&query_str)
        .fetch_all(pool)
        .await?;

    Ok(events)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    println!("Archive: Starting up");

    // -------------------------
    // Connect to NATS
    // -------------------------
    let nats_url = "nats://nats:4222";
    let client = ConnectOptions::new().connect(&nats_url).await?;
    println!("Archive connected to NATS at {}", nats_url);

    // -------------------------
    // Connect to DB
    // -------------------------
    let db_url = "postgres://signalgrid:signalgrid@postgres:5432/signalgrid";
    let pool = PgPoolOptions::new().max_connections(5).connect(&db_url).await?;
    println!("Archive: Connected to database");

    // -------------------------
    // Shared event buffer
    // -------------------------
    let event_buffer: Arc<tokio::sync::Mutex<Vec<Stats>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));

    // -------------------------
    // NATS subscription
    // -------------------------
    let sub = client.subscribe("stats").await?;
    let mut messages = sub;
    let buffer_clone = event_buffer.clone();

    tokio::spawn(async move {
        while let Some(msg) = messages.next().await {
            let payload: Bytes = msg.payload;
            if let Ok(event) = serde_json::from_slice::<Stats>(&payload) {
                // Add to buffer
                let mut buf = buffer_clone.lock().await;
                buf.push(event);
            } else {
                eprintln!("Archive: Failed to parse event");
            }
        }

        // Keep listener alive
        loop {
            eprintln!("Archive: NATS subscription ended, retrying in 5s...");
            time::sleep(Duration::from_secs(5)).await;
        }
    });

    // -------------------------
    // Flush buffer every 12 minutes
    // -------------------------
    let buffer_clone = event_buffer.clone();
    let pool_clone = pool.clone();
    tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(60));
        loop {
            interval.tick().await;

            let mut buf = buffer_clone.lock().await;
            if buf.is_empty() {
                continue;
            }

            println!("Flushing {} events to database", buf.len());

            for event in buf.drain(..) {
                if let Err(e) = sqlx::query(
                    "INSERT INTO stats ( id, station, sensor, timestamp, mean, min, max, count) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                )
                .bind(Uuid::new_v4().to_string())
                .bind(&event.station)
                .bind(&event.sensor)
                .bind(event.timestamp)
                .bind(event.mean)
                .bind(event.min)
                .bind(event.max)
                .bind(event.count)
                .execute(&pool_clone)
                .await
                {
                    eprintln!("Archive: Failed to insert event: {:?}", e);
                }
            }
        }
    });

    // -------------------------
    // HTTP endpoint for historical stats
    // ---
    let stats_route = warp::path("stats")
        .and(warp::get())
        .and(warp::query::<HashMap<String, String>>())
        .and(with_db(pool.clone())) // this is fine now
        .and_then(|params: HashMap<String, String>, pool: sqlx::PgPool| async move {
            let sensor = params.get("sensor").map(|s| s.as_str());
            let start_ts = params.get("start_ts").and_then(|s| s.parse::<i64>().ok());
            let end_ts = params.get("end_ts").and_then(|s| s.parse::<i64>().ok());

            match query_events(&pool, sensor, start_ts, end_ts).await {
                Ok(stats) => Ok::<_, Infallible>(warp::reply::json(&stats)),
                Err(e) => Ok::<_, Infallible>(warp::reply::json(&serde_json::json!({
                    "error": format!("Internal server error: {:?}", e)
                }))),
            }
        });


    println!("HTTP server running at http://0.0.0.0:8003");
    warp::serve(stats_route).run(([0, 0, 0, 0], 8003)).await;

    Ok(())
}
