//! At-least-once ingestion from a durable JetStream pull consumer.
//!
//! Messages are acknowledged only after the batch containing them is committed
//! to Postgres. A crash between commit and ack causes redelivery, which the
//! primary key turns into a no-op, so the end result is effectively-once.

use crate::db;
use async_nats::jetstream::consumer::{pull, AckPolicy, PullConsumer};
use async_nats::jetstream::{self, AckKind};
use futures::future::join_all;
use futures::StreamExt;
use metrics::{counter, histogram};
use signalgrid_core::subjects::{ensure_stats_stream, STATS_ALL};
use signalgrid_core::Stats;
use sqlx::PgPool;
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tracing::{error, info, warn};

pub const CONSUMER_NAME: &str = "archive";

#[derive(Debug, Clone, Copy)]
pub struct BatchConfig {
    pub max_messages: usize,
    pub max_wait: Duration,
}

pub async fn consumer(js: &jetstream::Context) -> anyhow::Result<PullConsumer> {
    let stream = ensure_stats_stream(js).await?;
    let consumer = stream
        .get_or_create_consumer(
            CONSUMER_NAME,
            pull::Config {
                durable_name: Some(CONSUMER_NAME.to_string()),
                filter_subject: STATS_ALL.to_string(),
                ack_policy: AckPolicy::Explicit,
                ack_wait: Duration::from_secs(30),
                max_ack_pending: 20_000,
                ..Default::default()
            },
        )
        .await?;
    Ok(consumer)
}

/// Runs until `shutdown` flips. The batch in flight is always finished
/// (committed and acked) before returning.
pub async fn run(
    consumer: PullConsumer,
    pool: PgPool,
    cfg: BatchConfig,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut messages = consumer
        .stream()
        .max_messages_per_batch(cfg.max_messages)
        .messages()
        .await?;
    let mut backoff = Duration::from_secs(1);
    info!(
        event = "ingest_started",
        consumer = CONSUMER_NAME,
        max_batch = cfg.max_messages
    );

    loop {
        // Wait for the first message of the next batch (or shutdown).
        let first = tokio::select! {
            _ = shutdown.changed() => break,
            next = messages.next() => match next {
                Some(Ok(msg)) => msg,
                Some(Err(e)) => {
                    warn!(event = "consumer_error", error = %e);
                    continue;
                }
                None => {
                    error!(event = "consumer_stream_ended");
                    break;
                }
            },
        };

        // Fill the batch until it is full or `max_wait` has elapsed.
        let mut batch = vec![first];
        let deadline = tokio::time::Instant::now() + cfg.max_wait;
        while batch.len() < cfg.max_messages {
            match tokio::time::timeout_at(deadline, messages.next()).await {
                Ok(Some(Ok(msg))) => batch.push(msg),
                Ok(Some(Err(e))) => warn!(event = "consumer_error", error = %e),
                Ok(None) | Err(_) => break,
            }
        }

        if commit(&pool, batch).await {
            backoff = Duration::from_secs(1);
        } else {
            tokio::select! {
                _ = shutdown.changed() => break,
                _ = tokio::time::sleep(backoff) => {}
            }
            backoff = (backoff * 2).min(Duration::from_secs(30));
        }
    }

    info!(event = "ingest_stopped");
    Ok(())
}

/// Writes one batch and acks it. Returns false if the database write failed
/// (the messages are NAKed for redelivery).
async fn commit(pool: &PgPool, batch: Vec<jetstream::Message>) -> bool {
    let mut stats = Vec::with_capacity(batch.len());
    let mut valid = Vec::with_capacity(batch.len());
    for msg in batch {
        match serde_json::from_slice::<Stats>(&msg.payload) {
            Ok(s) => {
                stats.push(s);
                valid.push(msg);
            }
            Err(e) => {
                // A poison message would otherwise be redelivered forever.
                counter!("archive_poison_messages_total").increment(1);
                warn!(event = "poison_message", subject = %msg.subject, error = %e);
                let _ = msg.ack_with(AckKind::Term).await;
            }
        }
    }
    if valid.is_empty() {
        return true;
    }

    let started = Instant::now();
    match db::insert_batch(pool, &stats).await {
        Ok(inserted) => {
            histogram!("archive_batch_write_seconds").record(started.elapsed().as_secs_f64());
            histogram!("archive_batch_size").record(valid.len() as f64);
            counter!("archive_rows_inserted_total").increment(inserted);
            counter!("archive_duplicates_skipped_total").increment(valid.len() as u64 - inserted);
            for result in join_all(valid.iter().map(|m| m.ack())).await {
                if let Err(e) = result {
                    warn!(event = "ack_failed", error = %e);
                }
            }
            true
        }
        Err(e) => {
            counter!("archive_batch_failures_total").increment(1);
            error!(event = "batch_write_failed", rows = valid.len(), error = %e);
            let delay = Some(Duration::from_secs(2));
            join_all(valid.iter().map(|m| m.ack_with(AckKind::Nak(delay)))).await;
            false
        }
    }
}
