use archive::ingest::{self, BatchConfig};
use archive::{db, http};
use async_nats::jetstream;
use signalgrid_core::config::{connect_nats, env_or, required};
use signalgrid_core::shutdown_signal;
use signalgrid_core::telemetry::{init_metrics, init_tracing};
use std::time::Duration;
use tokio::sync::watch;
use tracing::{error, info};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing("archive");
    init_metrics()?;

    let http_port: u16 = env_or("HTTP_PORT", 8003)?;
    let batch = BatchConfig {
        max_messages: env_or("BATCH_MAX_MESSAGES", 500)?,
        max_wait: Duration::from_millis(env_or("BATCH_MAX_WAIT_MS", 1_000)?),
    };

    let pool = db::connect(&required("DATABASE_URL")?).await?;
    let nats = connect_nats("archive").await?;
    let consumer = ingest::consumer(&jetstream::new(nats.clone())).await?;

    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let ingest_task = tokio::spawn(ingest::run(
        consumer,
        pool.clone(),
        batch,
        shutdown_rx.clone(),
    ));

    let mut http_shutdown = shutdown_rx.clone();
    let server = warp::serve(http::routes(pool.clone(), nats.clone()))
        .bind(([0, 0, 0, 0], http_port))
        .await
        .graceful(async move {
            let _ = http_shutdown.changed().await;
        });
    let server_task = tokio::spawn(server.run());
    info!(event = "http_listening", port = http_port);

    shutdown_signal().await;
    let _ = shutdown_tx.send(true);

    match ingest_task.await {
        Ok(Ok(())) => {}
        Ok(Err(e)) => error!(event = "ingest_failed", error = %e),
        Err(e) => error!(event = "ingest_join_failed", error = %e),
    }
    let _ = server_task.await;
    nats.drain().await?;
    pool.close().await;
    info!(event = "stopped");
    Ok(())
}
