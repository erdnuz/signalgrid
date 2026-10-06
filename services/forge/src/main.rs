use async_nats::jetstream::{self, message::PublishMessage};
use chrono::Utc;
use forge::{Ingest, WindowAggregator};
use futures::future::join_all;
use futures::StreamExt;
use metrics::{counter, gauge, histogram};
use signalgrid_core::config::{connect_nats, env_or};
use signalgrid_core::subjects::{self, ensure_stats_stream};
use signalgrid_core::telemetry::{init_metrics, init_tracing};
use signalgrid_core::{shutdown_signal, RawEvent, Stats};
use std::future::IntoFuture;
use tokio::time::{self, Duration, Instant, MissedTickBehavior};
use tracing::{error, info, warn};

/// Publishes closed windows to JetStream. The window identity is sent as
/// `Nats-Msg-Id`, so a re-publish after a crash is de-duplicated by the server.
async fn publish(js: &jetstream::Context, stats: Vec<Stats>) {
    if stats.is_empty() {
        return;
    }
    let now_ms = Utc::now().timestamp_millis();
    let mut acks = Vec::with_capacity(stats.len());

    for stat in &stats {
        histogram!("forge_window_emit_lag_ms")
            .record((now_ms - (stat.timestamp + stat.window_ms)) as f64);
        let payload = match serde_json::to_vec(stat) {
            Ok(p) => p,
            Err(e) => {
                error!(event = "encode_stats_failed", error = %e);
                continue;
            }
        };
        let message = PublishMessage::build()
            .payload(payload.into())
            .message_id(stat.message_id());
        match js
            .send_publish(subjects::stats(&stat.station, stat.sensor), message)
            .await
        {
            Ok(ack) => acks.push(ack),
            Err(e) => {
                counter!("forge_publish_failures_total").increment(1);
                error!(event = "publish_stats_failed", error = %e);
            }
        }
    }

    for result in join_all(acks.into_iter().map(IntoFuture::into_future)).await {
        match result {
            Ok(ack) if ack.duplicate => counter!("forge_publish_duplicates_total").increment(1),
            Ok(_) => counter!("forge_stats_published_total").increment(1),
            Err(e) => {
                counter!("forge_publish_failures_total").increment(1);
                error!(event = "publish_ack_failed", error = %e);
            }
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing("forge");
    init_metrics()?;

    let window_ms: i64 = env_or("WINDOW_MS", 500)?;
    let lateness_ms: i64 = env_or("ALLOWED_LATENESS_MS", 200)?;
    // Close everything if input stops, so the last windows are not held forever.
    let idle_flush = Duration::from_millis(env_or("IDLE_FLUSH_MS", 2_000)?);

    let client = connect_nats("forge").await?;
    let js = jetstream::new(client.clone());
    ensure_stats_stream(&js).await?;
    let mut sub = client.subscribe(subjects::RAW_ALL).await?;
    info!(
        event = "subscribed",
        subject = subjects::RAW_ALL,
        window_ms,
        lateness_ms
    );

    let mut agg = WindowAggregator::new(window_ms, lateness_ms);
    let mut ticker = time::interval(Duration::from_millis((window_ms as u64 / 5).max(10)));
    ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let mut last_event = Instant::now();
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        tokio::select! {
            _ = &mut shutdown => break,

            maybe_msg = sub.next() => {
                let Some(msg) = maybe_msg else {
                    warn!(event = "subscription_closed");
                    break;
                };
                match serde_json::from_slice::<RawEvent>(&msg.payload) {
                    Ok(event) => {
                        last_event = Instant::now();
                        match agg.ingest(&event) {
                            Ingest::Accepted { dropped_values } => {
                                counter!("forge_events_total").increment(1);
                                counter!("forge_non_finite_values_total").increment(dropped_values as u64);
                            }
                            Ingest::Late => counter!("forge_late_events_total").increment(1),
                        }
                    }
                    Err(e) => {
                        counter!("forge_parse_errors_total").increment(1);
                        error!(event = "parse_event_failed", error = %e);
                    }
                }
            }

            _ = ticker.tick() => {
                let ready = if last_event.elapsed() > idle_flush {
                    agg.close_all()
                } else {
                    agg.close_ready()
                };
                publish(&js, ready).await;
                gauge!("forge_open_windows").set(agg.open_windows() as f64);
            }
        }
    }

    info!(event = "draining", open_windows = agg.open_windows());
    publish(&js, agg.close_all()).await;
    client.flush().await?;
    Ok(())
}
