use anyhow::Context;
use async_nats::ConnectOptions;
use bytes::Bytes;
use chrono::Utc;
use forge::{aggregate_stats, prune_expired, Event};
use futures::stream::StreamExt;
use std::env;
use tokio::time::{self, Duration};
use tracing::{error, info, warn};

const INITIAL_AGGREGATION_DELAY_SECS: u64 = 5;
const AGGREGATION_INTERVAL_MS: u64 = 400;

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

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let nats_url = env::var("NATS_URL").context("NATS_URL environment variable must be set")?;
    let client = ConnectOptions::new()
        .retry_on_initial_connect()
        .connect(&nats_url)
        .await?;
    info!(service = "forge", event = "nats_connected", %nats_url);

    let mut sub = client.subscribe("events").await?;
    let mut buffer: Vec<Event> = Vec::new();
    let start = time::Instant::now() + Duration::from_secs(INITIAL_AGGREGATION_DELAY_SECS);
    let mut ticker = time::interval_at(start, Duration::from_millis(AGGREGATION_INTERVAL_MS));
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        tokio::select! {
            _ = &mut shutdown => {
                info!(service = "forge", event = "signal_received");
                break;
            }

            maybe_msg = sub.next() => {
                let Some(msg) = maybe_msg else {
                    warn!(service = "forge", event = "subscription_closed");
                    break;
                };
                match serde_json::from_slice::<Event>(&msg.payload) {
                    Ok(event) => buffer.push(event),
                    Err(e) => error!(service = "forge", event = "parse_event_failed", error = %e),
                }
            }

            _ = ticker.tick() => {
                let now_ms = Utc::now().timestamp_millis() as u64;
                prune_expired(&mut buffer, now_ms);
                for stat in aggregate_stats(&buffer, now_ms) {
                    match serde_json::to_vec(&stat) {
                        Ok(encoded) => {
                            let payload: Bytes = encoded.into();
                            if let Err(e) = client.publish("stats", payload).await {
                                error!(service = "forge", event = "publish_stats_failed", error = %e);
                            }
                        }
                        Err(e) => error!(service = "forge", event = "encode_stats_failed", error = %e),
                    }
                }
            }
        }
    }

    client.flush().await?;
    Ok(())
}
