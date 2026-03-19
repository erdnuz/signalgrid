use async_nats::ConnectOptions;
use bytes::Bytes;
use forge::{aggregate_stats, Event, Stats};
use futures::stream::StreamExt;
use std::sync::Arc;
use std::env;
use anyhow::Context;
use tokio::sync::watch;
use tokio::time::{self, Duration};
use chrono::Utc;

const INITIAL_AGGREGATION_DELAY_SECS: u64 = 5;
const AGGREGATION_INTERVAL_MS: u64 = 400;

use tracing::{info, error};
use tracing_subscriber;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;
    let client = ConnectOptions::new().connect(&nats_url).await?;
    info!(service = "forge", event = "nats_connected", %nats_url);

    let stats_history: Arc<tokio::sync::Mutex<Vec<Stats>>> = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let mut sub = client.subscribe("events").await?;
    let stats_history_clone = stats_history.clone();
    let client_clone = client.clone();
    let (shutdown_tx, mut shutdown_rx) = watch::channel(false);

    let aggregation_task = tokio::spawn(async move {
        let mut buffer: Vec<Event> = Vec::new();
        let mut sleep = Box::pin(time::sleep(Duration::from_secs(INITIAL_AGGREGATION_DELAY_SECS)));

        loop {
            tokio::select! {
                changed = shutdown_rx.changed() => {
                    if changed.is_ok() && *shutdown_rx.borrow() {
                        info!(service = "forge", event = "stopping_aggregation");
                        break;
                    }
                }

                maybe_msg = sub.next() => {
                    if let Some(msg) = maybe_msg {
                        if let Ok(event) = serde_json::from_slice::<Event>(&msg.payload) {
                            buffer.push(event);
                        } else {
                            error!(service = "forge", event = "parse_event_failed");
                        }
                    }
                }

                _ = &mut sleep => {
                    if !buffer.is_empty() {
                        let now_ms = Utc::now().timestamp_millis() as u64;
                        let stats_to_publish = aggregate_stats(&buffer, now_ms);

                        // Store history
                        {
                            let mut guard = stats_history_clone.lock().await;
                            guard.extend(stats_to_publish.clone());
                        }

                        // Publish stats
                        for stat in stats_to_publish {
                            match serde_json::to_vec(&stat) {
                                Ok(encoded) => {
                                    let payload: Bytes = encoded.into();
                                    if let Err(e) = client_clone.publish("stats", payload).await {
                                        error!(service = "forge", event = "publish_stats_failed", error = %e);
                                    }
                                }
                                Err(e) => {
                                    error!(service = "forge", event = "encode_stats_failed", error = %e);
                                }
                            }
                        }
                    }

                    // Reset sleep
                    sleep = Box::pin(time::sleep(Duration::from_millis(AGGREGATION_INTERVAL_MS)));
                }
            }
        }
    });

    tokio::signal::ctrl_c().await?;
    info!(service = "forge", event = "signal_received", signal = "ctrl_c");
    let _ = shutdown_tx.send(true);

    if let Err(e) = aggregation_task.await {
        error!(service = "forge", event = "task_join_error", error = %e);
    }

    Ok(())
}
