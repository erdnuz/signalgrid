use tracing_subscriber::{fmt, EnvFilter};

/// Initialises structured logging. `RUST_LOG` controls the filter (default
/// `info`) and `LOG_FORMAT=json` switches to one JSON object per line.
pub fn init_tracing(service: &'static str) {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let json = std::env::var("LOG_FORMAT").is_ok_and(|v| v.eq_ignore_ascii_case("json"));
    let builder = fmt().with_env_filter(filter).with_target(false);
    if json {
        builder.json().flatten_event(true).init();
    } else {
        builder.init();
    }
    tracing::info!(service, event = "starting");
}

/// Serves Prometheus metrics on `0.0.0.0:<METRICS_PORT>/metrics` (default 9000).
pub fn init_metrics() -> anyhow::Result<()> {
    let port: u16 = crate::config::env_or("METRICS_PORT", 9000)?;
    metrics_exporter_prometheus::PrometheusBuilder::new()
        .with_http_listener(([0, 0, 0, 0], port))
        .install()?;
    tracing::info!(event = "metrics_listening", port);
    Ok(())
}
