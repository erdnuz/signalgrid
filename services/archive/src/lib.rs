use std::collections::HashMap;

pub const DEFAULT_LIMIT: i64 = 1_000;
pub const MAX_LIMIT: i64 = 10_000;

#[derive(Debug, PartialEq, Eq)]
pub struct QueryParams {
    pub station: Option<String>,
    pub sensor: Option<i32>,
    pub start_ts: Option<i64>,
    pub end_ts: Option<i64>,
    pub limit: i64,
}

fn parse_opt<T: std::str::FromStr>(
    params: &HashMap<String, String>,
    key: &str,
) -> Result<Option<T>, String> {
    params
        .get(key)
        .map(|raw| {
            raw.parse::<T>()
                .map_err(|_| format!("invalid value for '{key}': {raw:?}"))
        })
        .transpose()
}

/// Parses and validates `/stats` query parameters. Malformed values are
/// rejected rather than silently ignored, so a typo can never widen a query.
pub fn parse_query_params(params: &HashMap<String, String>) -> Result<QueryParams, String> {
    let sensor = parse_opt::<i32>(params, "sensor")?;
    let start_ts = parse_opt::<i64>(params, "start_ts")?;
    let end_ts = parse_opt::<i64>(params, "end_ts")?;
    let limit = parse_opt::<i64>(params, "limit")?.unwrap_or(DEFAULT_LIMIT);

    if !(1..=MAX_LIMIT).contains(&limit) {
        return Err(format!("'limit' must be between 1 and {MAX_LIMIT}"));
    }
    if let (Some(start), Some(end)) = (start_ts, end_ts) {
        if start > end {
            return Err("'start_ts' must not be after 'end_ts'".to_string());
        }
    }

    Ok(QueryParams {
        station: params.get("station").cloned(),
        sensor,
        start_ts,
        end_ts,
        limit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn parses_valid_values() {
        let parsed = parse_query_params(&params(&[
            ("station", "StationA"),
            ("sensor", "2"),
            ("start_ts", "100"),
            ("end_ts", "200"),
            ("limit", "50"),
        ]))
        .unwrap();

        assert_eq!(
            parsed,
            QueryParams {
                station: Some("StationA".to_string()),
                sensor: Some(2),
                start_ts: Some(100),
                end_ts: Some(200),
                limit: 50,
            }
        );
    }

    #[test]
    fn applies_default_limit() {
        let parsed = parse_query_params(&HashMap::new()).unwrap();
        assert_eq!(parsed.limit, DEFAULT_LIMIT);
    }

    #[test]
    fn rejects_invalid_numeric_values() {
        assert!(parse_query_params(&params(&[("sensor", "abc")])).is_err());
        assert!(parse_query_params(&params(&[("start_ts", "x")])).is_err());
    }

    #[test]
    fn rejects_out_of_range_limit() {
        assert!(parse_query_params(&params(&[("limit", "0")])).is_err());
        assert!(parse_query_params(&params(&[("limit", "1000000")])).is_err());
    }

    #[test]
    fn rejects_inverted_range() {
        assert!(parse_query_params(&params(&[("start_ts", "200"), ("end_ts", "100")])).is_err());
    }
}
