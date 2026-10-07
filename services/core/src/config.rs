use anyhow::{anyhow, Context};
use std::str::FromStr;

/// Reads a required environment variable.
pub fn required(key: &str) -> anyhow::Result<String> {
    std::env::var(key).with_context(|| format!("{key} environment variable must be set"))
}

/// Reads and parses an optional environment variable, falling back to `default`.
pub fn env_or<T>(key: &str, default: T) -> anyhow::Result<T>
where
    T: FromStr,
    T::Err: std::fmt::Display,
{
    match std::env::var(key) {
        Ok(raw) => raw
            .parse::<T>()
            .map_err(|e| anyhow!("invalid value for {key}={raw:?}: {e}")),
        Err(_) => Ok(default),
    }
}

/// Connects to NATS, retrying until the server is reachable so services do not
/// depend on container start order.
pub async fn connect_nats(service: &str) -> anyhow::Result<async_nats::Client> {
    let url = required("NATS_URL")?;
    let client = async_nats::ConnectOptions::new()
        .name(service)
        .retry_on_initial_connect()
        .connect(&url)
        .await?;
    tracing::info!(event = "nats_connected", nats_url = %url);
    Ok(client)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_or_uses_default_when_unset() {
        assert_eq!(env_or::<u64>("SG_TEST_SURELY_UNSET", 7).unwrap(), 7);
    }
}
