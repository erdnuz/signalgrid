use anyhow::Context;
use archive::{parse_query_params, QueryParams};
use async_nats::ConnectOptions;
use futures::stream::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use sqlx::{FromRow, PgPool, Postgres, QueryBuilder};
use std::collections::HashMap;
use std::convert::Infallible;
use std::env;
use std::sync::Arc;
use tokio::sync::{watch, Mutex};
use tokio::time::{self, Duration};
use tracing::{error, info, warn};
use uuid::Uuid;
use warp::http::StatusCode;
use warp::Filter;

const DB_FLUSH_INTERVAL_SECS: u64 = 2;
const INITIAL_RETRY_BACKOFF_SECS: u64 = 1;
const MAX_RETRY_BACKOFF_SECS: u64 = 30;
const HTTP_PORT: u16 = 8003;

type Buffer = Arc<Mutex<Vec<Stats>>>;

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

fn with_db(pool: PgPool) -> impl Filter<Extract = (PgPool,), Error = Infallible> + Clone {
    warp::any().map(move || pool.clone())
}

/// Returns the most recent `limit` rows matching the filters, oldest first.
async fn query_stats(pool: &PgPool, q: &QueryParams) -> anyhow::Result<Vec<Stats>> {
    let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
        "SELECT station, sensor, timestamp, mean, min, max, count FROM stats WHERE TRUE",
    );
    if let Some(station) = &q.station {
        qb.push(" AND station = ").push_bind(station);
    }
    if let Some(sensor) = q.sensor {
        qb.push(" AND sensor = ").push_bind(sensor);
    }
    if let Some(start) = q.start_ts {
        qb.push(" AND timestamp >= ").push_bind(start);
    }
    if let Some(end) = q.end_ts {
        qb.push(" AND timestamp <= ").push_bind(end);
    }
    qb.push(" ORDER BY timestamp DESC LIMIT ").push_bind(q.limit);

    let mut rows = qb.build_query_as::<Stats>().fetch_all(pool).await?;
    rows.reverse();
    Ok(rows)
}

const INSERT_STATS_SQL: &str = "INSERT INTO stats (id, station, sensor, timestamp, mean, min, max, count) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)";

/// Writes everything currently buffered; rows that fail are put back for the next attempt.
async fn flush(pool: &PgPool, buffer: &Buffer) {
    let pending: Vec<Stats> = std::mem::take(&mut *buffer.lock().await);
    if pending.is_empty() {
        return;
    }
    info!(service = "archive", event = "flushing_events", count = pending.len());

    let mut failed = Vec::new();
    for stat in pending {
        if let Err(e) = sqlx::query(INSERT_STATS_SQL)
            .bind(Uuid::new_v4().to_string())
            .bind(&stat.station)
            .bind(stat.sensor)
            .bind(stat.timestamp)
            .bind(stat.mean)
            .bind(stat.min)
            .bind(stat.max)
            .bind(stat.count)
            .execute(pool)
            .await
        {
            error!(service = "archive", event = "insert_failed", error = %e);
            failed.push(stat);
        }
    }

    if !failed.is_empty() {
        warn!(service = "archive", event = "requeued_events", count = failed.len());
        let mut buf = buffer.lock().await;
        failed.append(&mut buf);
        *buf = failed;
    }
}

async fn connect_db_with_retry(db_url: &str) -> PgPool {
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

fn json_status<T: Serialize>(body: &T, status: StatusCode) -> warp::reply::WithStatus<warp::reply::Json> {
    warp::reply::with_status(warp::reply::json(body), status)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();
    info!(service = "archive", event = "starting_up");

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;
    let db_url = env::var("DATABASE_URL").context("DATABASE_URL environment variable must be set")?;
    let pool = connect_db_with_retry(&db_url).await;

    let nats = ConnectOptions::new()
        .retry_on_initial_connect()
        .connect(&nats_url)
        .await?;
    info!(service = "archive", event = "nats_connected", %nats_url);
    let mut messages = nats.subscribe("stats").await?;

    let buffer: Buffer = Arc::new(Mutex::new(Vec::new()));
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let ingest_buffer = buffer.clone();
    let mut ingest_shutdown = shutdown_rx.clone();
    let ingest_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = ingest_shutdown.changed() => break,
                maybe_msg = messages.next() => {
                    let Some(msg) = maybe_msg else {
                        error!(service = "archive", event = "nats_subscription_ended");
                        break;
                    };
                    match serde_json::from_slice::<Stats>(&msg.payload) {
                        Ok(stat) => ingest_buffer.lock().await.push(stat),
                        Err(e) => error!(service = "archive", event = "parse_event_failed", error = %e),
                    }
                }
            }
        }
    });

    let flush_buffer = buffer.clone();
    let flush_pool = pool.clone();
    let mut flush_shutdown = shutdown_rx.clone();
    let flush_task = tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_secs(DB_FLUSH_INTERVAL_SECS));
        loop {
            tokio::select! {
                _ = flush_shutdown.changed() => break,
                _ = interval.tick() => flush(&flush_pool, &flush_buffer).await,
            }
        }
    });

    let health = warp::path("health")
        .and(warp::get())
        .map(|| warp::reply::json(&serde_json::json!({"status": "ok", "service": "archive"})));

    let ready = warp::path("ready")
        .and(warp::get())
        .and(with_db(pool.clone()))
        .then(|pool: PgPool| async move {
            let ok = sqlx::query_scalar::<_, i32>("SELECT 1").fetch_one(&pool).await.is_ok();
            let (label, status) = if ok {
                ("ready", StatusCode::OK)
            } else {
                ("not_ready", StatusCode::SERVICE_UNAVAILABLE)
            };
            json_status(&serde_json::json!({"status": label, "service": "archive"}), status)
        });

    let stats = warp::path("stats")
        .and(warp::get())
        .and(warp::query::<HashMap<String, String>>())
        .and(with_db(pool.clone()))
        .then(|params: HashMap<String, String>, pool: PgPool| async move {
            let q = match parse_query_params(&params) {
                Ok(q) => q,
                Err(msg) => return json_status(&serde_json::json!({"error": msg}), StatusCode::BAD_REQUEST),
            };
            match query_stats(&pool, &q).await {
                Ok(rows) => json_status(&rows, StatusCode::OK),
                Err(e) => {
                    error!(service = "archive", event = "query_failed", error = %e);
                    json_status(
                        &serde_json::json!({"error": "internal server error"}),
                        StatusCode::INTERNAL_SERVER_ERROR,
                    )
                }
            }
        });

    let routes = health.or(ready).or(stats);
    info!(service = "archive", event = "http_server_start", port = HTTP_PORT);
    warp::serve(routes)
        .bind(([0, 0, 0, 0], HTTP_PORT))
        .await
        .graceful(shutdown_signal())
        .run()
        .await;

    info!(service = "archive", event = "shutting_down");
    let _ = shutdown_tx.send(true);
    for (name, task) in [("ingest", ingest_task), ("flush", flush_task)] {
        if let Err(e) = task.await {
            error!(service = "archive", event = "task_join_error", task = name, error = %e);
        }
    }

    // Ingest has stopped, so this drains everything received before shutdown.
    flush(&pool, &buffer).await;
    nats.drain().await?;
    Ok(())
}
