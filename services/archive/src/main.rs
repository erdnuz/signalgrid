use async_nats::ConnectOptions;
use archive::parse_query_params;
use bytes::Bytes;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use std::sync::Arc;
use tokio::sync::watch;
use tokio::time::{self, Duration};
use sqlx::{FromRow, Postgres, QueryBuilder};
use warp::Filter;
use std::convert::Infallible;
use std::collections::HashMap;
use std::env;
use anyhow::Context;
use tracing::{info, error};
use tracing_subscriber;
use uuid::Uuid;

const DB_FLUSH_INTERVAL_SECS: u64 = 60;
const INITIAL_RETRY_BACKOFF_SECS: u64 = 1;
const MAX_RETRY_BACKOFF_SECS: u64 = 30;

// Helper to pass DB pool into warp filters
fn with_db(
    pool: sqlx::PgPool,
) -> impl Filter<Extract = (sqlx::PgPool,), Error = Infallible> + Clone {
    warp::any().map(move || pool.clone())
}


#[derive(Debug, Serialize, Deserialize, Clone, FromRow)]
struct Stats {
    station: String,
    sensor: i32,
    mean: f64,
    min: f64,
    max: f64,
    count: i64,
    timestamp: i64,
}

// Query historical events
async fn query_events(
    pool: &sqlx::PgPool,
    station: Option<&str>,
    sensor: Option<i32>,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
) -> anyhow::Result<Vec<Stats>> {
    let mut query_builder: QueryBuilder<Postgres> = QueryBuilder::new(
        "SELECT station, sensor, timestamp, mean, min, max, count FROM stats",
    );

    let mut has_where = false;

    if let Some(station_value) = station {
        if !has_where {
            query_builder.push(" WHERE ");
            has_where = true;
        } else {
            query_builder.push(" AND ");
        }
        query_builder.push("station = ").push_bind(station_value);
    }

    if let Some(sensor_value) = sensor {
        if !has_where {
            query_builder.push(" WHERE ");
            has_where = true;
        } else {
            query_builder.push(" AND ");
        }
        query_builder.push("sensor = ").push_bind(sensor_value);
    }

    if let Some(start_value) = start_ts {
        if !has_where {
            query_builder.push(" WHERE ");
            has_where = true;
        } else {
            query_builder.push(" AND ");
        }
        query_builder.push("timestamp >= ").push_bind(start_value);
    }

    if let Some(end_value) = end_ts {
        if !has_where {
            query_builder.push(" WHERE ");
        } else {
            query_builder.push(" AND ");
        }
        query_builder.push("timestamp <= ").push_bind(end_value);
    }

    let events = query_builder
        .build_query_as::<Stats>()
        .fetch_all(pool)
        .await?;

    Ok(events)
}

const INSERT_STATS_SQL: &str = "INSERT INTO stats (id, station, sensor, timestamp, mean, min, max, count) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)";

async fn connect_db_with_retry(db_url: &str) -> sqlx::PgPool {
    let mut backoff_secs = INITIAL_RETRY_BACKOFF_SECS;

    loop {
        match PgPoolOptions::new().max_connections(5).connect(db_url).await {
            Ok(pool) => {
                info!(service = "archive", event = "db_connected");
                return pool;
            }
            Err(e) => {
                error!(service = "archive", event = "db_connect_failed", error = %e, backoff_secs);
                time::sleep(Duration::from_secs(backoff_secs)).await;
                backoff_secs = (backoff_secs * 2).min(MAX_RETRY_BACKOFF_SECS);
            }
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();
    info!(service = "archive", event = "starting_up");

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;

    let db_url = env::var("DATABASE_URL").context("DATABASE_URL environment variable must be set")?;
    let pool = connect_db_with_retry(&db_url).await;

    // -------------------------
    // Shared event buffer
    // -------------------------
    let event_buffer: Arc<tokio::sync::Mutex<Vec<Stats>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));

    // -------------------------
    // NATS subscription
    // -------------------------
    let buffer_clone = event_buffer.clone();
    let nats_url_clone = nats_url.clone();
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let mut nats_shutdown_rx = shutdown_rx.clone();

    let nats_task = tokio::spawn(async move {
        let mut backoff_secs = INITIAL_RETRY_BACKOFF_SECS;

        loop {
            if *nats_shutdown_rx.borrow() {
                info!(service = "archive", event = "stopping_nats_subscriber");
                break;
            }

            match ConnectOptions::new().connect(&nats_url_clone).await {
                Ok(client) => {
                    info!(service = "archive", event = "nats_connected", %nats_url_clone);
                    backoff_secs = INITIAL_RETRY_BACKOFF_SECS;

                    match client.subscribe("stats").await {
                        Ok(mut messages) => {
                            loop {
                                tokio::select! {
                                    changed = nats_shutdown_rx.changed() => {
                                        if changed.is_ok() && *nats_shutdown_rx.borrow() {
                                            info!(service = "archive", event = "stopping_nats_subscriber");
                                            return;
                                        }
                                    }
                                    maybe_msg = messages.next() => {
                                        match maybe_msg {
                                            Some(msg) => {
                                                let payload: Bytes = msg.payload;
                                                if let Ok(event) = serde_json::from_slice::<Stats>(&payload) {
                                                    let mut buf = buffer_clone.lock().await;
                                                    buf.push(event);
                                                } else {
                                                    error!(service = "archive", event = "parse_event_failed");
                                                }
                                            }
                                            None => {
                                                error!(service = "archive", event = "nats_subscription_ended");
                                                break;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        Err(e) => {
                            error!(service = "archive", event = "subscribe_failed", error = %e);
                        }
                    }
                }
                Err(e) => {
                    error!(service = "archive", event = "nats_connect_failed", error = %e);
                }
            }

            info!(service = "archive", event = "nats_retry", backoff_secs);
            tokio::select! {
                changed = nats_shutdown_rx.changed() => {
                    if changed.is_ok() && *nats_shutdown_rx.borrow() {
                        info!(service = "archive", event = "stopping_nats_subscriber");
                        break;
                    }
                }
                _ = time::sleep(Duration::from_secs(backoff_secs)) => {}
            }
            backoff_secs = (backoff_secs * 2).min(MAX_RETRY_BACKOFF_SECS);
        }
    });

    let buffer_clone = event_buffer.clone();
    let pool_clone = pool.clone();
    let mut flush_shutdown_rx = shutdown_rx.clone();
    let flush_task = tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(DB_FLUSH_INTERVAL_SECS));
        loop {
            tokio::select! {
                changed = flush_shutdown_rx.changed() => {
                    if changed.is_ok() && *flush_shutdown_rx.borrow() {
                        info!(service = "archive", event = "stopping_db_flush");
                        break;
                    }
                }
                _ = interval.tick() => {}
            }

            let events_to_flush = {
                let mut buf = buffer_clone.lock().await;
                if buf.is_empty() {
                    Vec::new()
                } else {
                    buf.drain(..).collect::<Vec<Stats>>()
                }
            };

            if events_to_flush.is_empty() {
                continue;
            }

            info!(service = "archive", event = "flushing_events", count = events_to_flush.len());

            let mut failed_events = Vec::new();

            for event in events_to_flush {
                if let Err(e) = sqlx::query(INSERT_STATS_SQL)
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
                    error!(service = "archive", event = "insert_failed", error = %e);
                    failed_events.push(event);
                }
            }

            if !failed_events.is_empty() {
                let failed_count = failed_events.len();
                let mut buf = buffer_clone.lock().await;
                failed_events.extend(buf.drain(..));
                *buf = failed_events;

                error!(service = "archive", event = "requeued_events", count = failed_count);
            }
        }
    });

    // -------------------------
    // Health and readiness endpoints
    // -------------------------
    let health_route = warp::path("health")
        .and(warp::get())
        .map(|| {
            warp::reply::json(&serde_json::json!({
                "status": "ok",
                "service": "archive"
            }))
        });

    let ready_route = warp::path("ready")
        .and(warp::get())
        .and(with_db(pool.clone()))
        .and_then(|pool: sqlx::PgPool| async move {
            let is_ready = sqlx::query_scalar::<_, i32>("SELECT 1")
                .fetch_one(&pool)
                .await
                .is_ok();

            Ok::<_, Infallible>(warp::reply::json(&serde_json::json!({
                "status": if is_ready { "ready" } else { "not_ready" },
                "service": "archive"
            })))
        });

    // -------------------------
    // HTTP endpoint for historical stats
    // ---
    let stats_route = warp::path("stats")
        .and(warp::get())
        .and(warp::query::<HashMap<String, String>>())
        .and(with_db(pool.clone())) // this is fine now
        .and_then(|params: HashMap<String, String>, pool: sqlx::PgPool| async move {
            let query_params = parse_query_params(&params);
            let station = query_params.station.as_deref();
            let sensor = query_params.sensor;
            let start_ts = query_params.start_ts;
            let end_ts = query_params.end_ts;

            match query_events(&pool, station, sensor, start_ts, end_ts).await {
                Ok(stats) => Ok::<_, Infallible>(warp::reply::json(&stats)),
                Err(e) => Ok::<_, Infallible>(warp::reply::json(&serde_json::json!({
                    "error": format!("Internal server error: {:?}", e)
                }))),
            }
        });

    let routes = health_route.or(ready_route).or(stats_route);

    info!(service = "archive", event = "http_server_start", addr = "http://0.0.0.0:8003");
    tokio::select! {
        _ = warp::serve(routes).run(([0, 0, 0, 0], 8003)) => {
            error!(service = "archive", event = "http_server_stopped_unexpectedly");
        }
        ctrl = tokio::signal::ctrl_c() => {
            if let Err(e) = ctrl {
                error!(service = "archive", event = "signal_listen_failed", error = %e);
            }
            info!(service = "archive", event = "shutting_down");
        }
    }

    let _ = shutdown_tx.send(true);

    if let Err(e) = nats_task.await {
        eprintln!("Archive: NATS task join error: {:?}", e);
    }

    if let Err(e) = flush_task.await {
        eprintln!("Archive: DB flush task join error: {:?}", e);
    }

    Ok(())
}
