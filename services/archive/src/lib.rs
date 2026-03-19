use std::collections::HashMap;

#[derive(Debug, PartialEq, Eq)]
pub struct QueryParams {
    pub station: Option<String>,
    pub sensor: Option<i32>,
    pub start_ts: Option<i64>,
    pub end_ts: Option<i64>,
}

pub fn parse_query_params(params: &HashMap<String, String>) -> QueryParams {
    QueryParams {
        station: params.get("station").cloned(),
        sensor: params.get("sensor").and_then(|s| s.parse::<i32>().ok()),
        start_ts: params.get("start_ts").and_then(|s| s.parse::<i64>().ok()),
        end_ts: params.get("end_ts").and_then(|s| s.parse::<i64>().ok()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_query_params_parses_valid_values() {
        let mut params = HashMap::new();
        params.insert("station".to_string(), "StationA".to_string());
        params.insert("sensor".to_string(), "2".to_string());
        params.insert("start_ts".to_string(), "100".to_string());
        params.insert("end_ts".to_string(), "200".to_string());

        let parsed = parse_query_params(&params);

        assert_eq!(
            parsed,
            QueryParams {
                station: Some("StationA".to_string()),
                sensor: Some(2),
                start_ts: Some(100),
                end_ts: Some(200),
            }
        );
    }

    #[test]
    fn parse_query_params_ignores_invalid_numeric_values() {
        let mut params = HashMap::new();
        params.insert("sensor".to_string(), "abc".to_string());
        params.insert("start_ts".to_string(), "x".to_string());
        params.insert("end_ts".to_string(), "42".to_string());

        let parsed = parse_query_params(&params);

        assert_eq!(parsed.station, None);
        assert_eq!(parsed.sensor, None);
        assert_eq!(parsed.start_ts, None);
        assert_eq!(parsed.end_ts, Some(42));
    }
}
