use std::collections::HashMap;

use archive::parse_query_params;

#[test]
fn accepts_partial_filters() {
    let mut params = HashMap::new();
    params.insert("station".to_string(), "StationB".to_string());
    params.insert("start_ts".to_string(), "1700000".to_string());

    let parsed = parse_query_params(&params).unwrap();

    assert_eq!(parsed.station.as_deref(), Some("StationB"));
    assert_eq!(parsed.sensor, None);
    assert_eq!(parsed.start_ts, Some(1_700_000));
    assert_eq!(parsed.end_ts, None);
}

#[test]
fn error_message_names_the_bad_parameter() {
    let mut params = HashMap::new();
    params.insert("sensor".to_string(), "not-a-number".to_string());

    let err = parse_query_params(&params).unwrap_err();
    assert!(err.contains("sensor"));
}
