use crate::{db, parse_query_params};
use serde::Serialize;
use sqlx::PgPool;
use std::collections::HashMap;
use std::convert::Infallible;
use tracing::error;
use warp::http::StatusCode;
use warp::reply::{Json, WithStatus};
use warp::Filter;

fn json_status<T: Serialize>(body: &T, status: StatusCode) -> WithStatus<Json> {
    warp::reply::with_status(warp::reply::json(body), status)
}

fn with<T: Clone + Send>(value: T) -> impl Filter<Extract = (T,), Error = Infallible> + Clone {
    warp::any().map(move || value.clone())
}

/// `GET /health` (liveness), `GET /ready` (DB + NATS reachable) and
/// `GET /stats?station=&sensor=&start_ts=&end_ts=&limit=`.
pub fn routes(
    pool: PgPool,
    nats: async_nats::Client,
) -> impl Filter<Extract = (impl warp::Reply,), Error = warp::Rejection> + Clone {
    let health = warp::path("health")
        .and(warp::get())
        .map(|| warp::reply::json(&serde_json::json!({"status": "ok", "service": "archive"})));

    let ready = warp::path("ready")
        .and(warp::get())
        .and(with(pool.clone()))
        .and(with(nats))
        .then(|pool: PgPool, nats: async_nats::Client| async move {
            let db_ok = sqlx::query_scalar::<_, i32>("SELECT 1")
                .fetch_one(&pool)
                .await
                .is_ok();
            let nats_ok = nats.connection_state() == async_nats::connection::State::Connected;
            let status = if db_ok && nats_ok {
                StatusCode::OK
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            };
            json_status(
                &serde_json::json!({
                    "status": if status == StatusCode::OK { "ready" } else { "not_ready" },
                    "service": "archive",
                    "checks": {"postgres": db_ok, "nats": nats_ok},
                }),
                status,
            )
        });

    let stats = warp::path("stats")
        .and(warp::get())
        .and(warp::query::<HashMap<String, String>>())
        .and(with(pool))
        .then(|params: HashMap<String, String>, pool: PgPool| async move {
            let query = match parse_query_params(&params) {
                Ok(q) => q,
                Err(msg) => {
                    return json_status(&serde_json::json!({"error": msg}), StatusCode::BAD_REQUEST)
                }
            };
            match db::query_stats(&pool, &query).await {
                Ok(rows) => json_status(&rows, StatusCode::OK),
                Err(e) => {
                    // Log the detail, return a generic message.
                    error!(event = "query_failed", error = %e);
                    json_status(
                        &serde_json::json!({"error": "internal server error"}),
                        StatusCode::INTERNAL_SERVER_ERROR,
                    )
                }
            }
        });

    health.or(ready).or(stats)
}
