//! NATS subject hierarchy. Hierarchical subjects let consumers subscribe with
//! wildcards (`sg.stats.StationA.>`) instead of filtering in application code.
//!
//! ```text
//! sg.raw.<station>                 raw samples        (pulse    -> forge, forecast)
//! sg.stats.<station>.<sensor>      window statistics  (forge    -> archive, forecast, frontend)
//! sg.forecasts.<station>.<sensor>  forecasts          (forecast -> frontend)
//! sg.regimes.<station>             regime estimates   (forecast -> frontend)
//! ```

pub const RAW_ALL: &str = "sg.raw.>";
pub const STATS_ALL: &str = "sg.stats.>";

/// JetStream stream that durably captures every stats message.
pub const STATS_STREAM: &str = "SG_STATS";

pub fn raw(station: &str) -> String {
    format!("sg.raw.{station}")
}

pub fn stats(station: &str, sensor: u32) -> String {
    format!("sg.stats.{station}.{sensor}")
}

/// Creates the stats stream if it does not exist. Called by both the producer
/// (forge) and the consumer (archive) so start-up order does not matter.
/// `duplicate_window` lets the server drop re-published windows that carry
/// the same `Nats-Msg-Id`.
pub async fn ensure_stats_stream(
    js: &async_nats::jetstream::Context,
) -> anyhow::Result<async_nats::jetstream::stream::Stream> {
    use async_nats::jetstream::stream::{Config, StorageType};
    use std::time::Duration;

    let stream = js
        .get_or_create_stream(Config {
            name: STATS_STREAM.to_string(),
            subjects: vec![STATS_ALL.to_string()],
            storage: StorageType::File,
            max_age: Duration::from_secs(24 * 60 * 60),
            duplicate_window: Duration::from_secs(120),
            ..Default::default()
        })
        .await?;
    Ok(stream)
}

/// A station name must be a single NATS subject token.
pub fn is_valid_token(token: &str) -> bool {
    !token.is_empty()
        && token
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_hierarchical_subjects() {
        assert_eq!(raw("StationA"), "sg.raw.StationA");
        assert_eq!(stats("StationA", 3), "sg.stats.StationA.3");
    }

    #[test]
    fn rejects_tokens_that_would_break_the_hierarchy() {
        assert!(is_valid_token("Station_A-1"));
        for bad in ["", "a.b", "a b", "*", ">"] {
            assert!(!is_valid_token(bad), "{bad:?}");
        }
    }
}
