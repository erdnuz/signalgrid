from helpers import alert, forecast, regime, stats


def test_add_stats_reports_new_series_once(store):
    assert store.add_stats(stats(0)) is True
    assert store.add_stats(stats(500)) is False


def test_buffers_are_bounded(store):
    for i in range(store.max_points + 25):
        store.add_stats(stats(i * 500, float(i)))
    view = store.series("StationA", 0)
    assert len(view.timestamps) == store.max_points
    assert view.actuals[-1] == float(store.max_points + 24)


def test_duplicate_and_out_of_order_windows_ignored(store):
    store.add_stats(stats(1_000, 1.0))
    store.add_stats(stats(1_000, 9.0))
    store.add_stats(stats(500, 9.0))
    assert store.series("StationA", 0).actuals == [1.0]


def test_backfill_prepends_only_older_rows(store):
    store.add_stats(stats(5_000, 5.0))
    history = [stats(t, float(t)) for t in (4_000, 4_500, 5_000, 5_500)]
    assert store.backfill(("StationA", 0), history) == 2
    assert store.series("StationA", 0).timestamps == [4_000, 4_500, 5_000]


def test_forecast_view_separates_one_step_history_and_horizon(store):
    store.add_stats(stats(0))
    store.add_forecast(forecast(0))
    store.add_forecast(forecast(500))
    store.add_forecast(forecast(500))  # duplicate origin ignored
    view = store.series("StationA", 0)
    assert view.one_step_ts == [500, 1_000]
    assert view.horizon_ts == [1_000, 1_500, 2_000]
    assert view.confidence == 0.98
    assert view.metric_ts == [0, 500]
    assert view.mase == {1: [0.9, 0.9], 3: [0.8, 0.8]}
    assert view.metrics[3].coverage == 0.99


def test_backfill_bumps_epoch_only_when_rows_are_added(store):
    store.add_stats(stats(1_000))
    assert store.series("StationA", 0).epoch == 0
    store.backfill(("StationA", 0), [stats(500)])
    assert store.series("StationA", 0).epoch == 1
    store.backfill(("StationA", 0), [stats(2_000)])  # newer than live: ignored
    assert store.series("StationA", 0).epoch == 1


def test_latency_history_is_bucketed_per_second(store):
    for received in (1_100, 1_400, 2_100):
        store.add_stats(stats(received - 600, station="S", sensor=received), received_ms=received)
    ts, values = store.latency_history()
    assert ts == [1_000]  # the 2 s bucket is still open
    assert values == [100.0]


def test_kpis_average_over_the_selection(store):
    store.add_stats(stats(0), received_ms=600)
    store.add_stats(stats(0, sensor=1), received_ms=600)
    store.add_forecast(forecast(0))
    store.add_forecast(forecast(0, sensor=1))
    store.add_regime(regime(0, true=1, mapped=1))
    k = store.kpis([("StationA", 0), ("StationA", 1)], ["StationA"])
    assert k.latency_ms_p50 == 100  # received at 600, window closed at 500
    assert k.mase == {1: 0.9, 3: 0.8}
    assert k.coverage[1] == 0.97 and k.regime_accuracy == 0.9
    assert k.windows_per_s > 0


def test_alert_state_tracking(store):
    store.add_alert(alert(rule="calibration", severity="serious", ts=1_000))
    store.add_alert(alert(rule="anomaly", ts=1_000))
    log, active = store.alerts(now_ms=2_000)
    assert len(log) == 2 and len(active) == 2
    # One-shot events expire; state-based alerts stay until resolved.
    _, active = store.alerts(now_ms=60_000)
    assert [a.rule for a in active] == ["calibration"]
    store.add_alert(alert(rule="calibration", severity="info", state="resolved", ts=3_000))
    _, active = store.alerts(now_ms=60_000)
    assert active == []


def test_stale_series_raise_a_derived_alert(store):
    store.add_stats(stats(0), received_ms=1_000)
    assert store.alerts(now_ms=2_000)[1] == []
    _, active = store.alerts(now_ms=10_000)
    assert [(a.rule, a.severity) for a in active] == [("stale", "critical")]


def test_sensors_filter_by_station(store):
    store.add_stats(stats(0, station="StationA", sensor=1))
    store.add_stats(stats(0, station="StationA", sensor=0))
    store.add_stats(stats(0, station="StationB", sensor=2))
    assert store.stations() == ["StationA", "StationB"]
    assert store.sensors("StationA") == [0, 1]


def test_regimes_can_be_cropped_to_the_plotted_range(store):
    for ts in (0, 1_000, 2_000):
        store.add_regime(regime(ts, true=0, mapped=0))
    assert store.regimes("StationA", since=1_000).timestamps == [1_000, 2_000]
    assert store.regimes("StationA", since=5_000) is None


def test_latency_alert_needs_a_sustained_breach(store):
    # 29 slow seconds then a fast one: no alert. 30 slow seconds: alert.
    for sec in range(31):
        lat = 50 if sec == 29 else 300
        store.add_stats(stats(sec * 1_000, sensor=sec), received_ms=sec * 1_000 + 500 + lat)
    assert not [a for a in store.alerts(now_ms=31_000)[1] if a.rule == "latency"]
    for sec in range(31, 62):
        store.add_stats(stats(sec * 1_000, sensor=sec), received_ms=sec * 1_000 + 800)
    latency = [a for a in store.alerts(now_ms=62_000)[1] if a.rule == "latency"]
    assert latency and latency[0].severity == "warning"
