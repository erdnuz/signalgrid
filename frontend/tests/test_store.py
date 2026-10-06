from helpers import forecast, regime, stats


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


def test_forecast_view_joins_one_step_history_and_horizon(store):
    store.add_stats(stats(0))
    store.add_forecast(forecast(0))
    store.add_forecast(forecast(500))
    store.add_forecast(forecast(500))  # duplicate origin ignored
    view = store.series("StationA", 0)
    # two 1-step forecasts (t=500, t=1000) + the two remaining horizon steps
    assert view.forecast_ts == [500, 1_000, 1_500, 2_000]
    assert view.confidence == 0.98


def test_kpis(store):
    store.add_stats(stats(0), received_ms=600)
    store.add_forecast(forecast(0))
    store.add_regime(regime(0, true=1, mapped=1))
    k = store.kpis("StationA", 0)
    assert k.latency_ms_p50 == 100  # received at 600, window closed at 500
    assert (k.mae, k.coverage, k.regime_accuracy) == (0.1, 0.97, 0.9)
    assert k.windows_per_s > 0


def test_sensors_filter_by_station(store):
    store.add_stats(stats(0, station="StationA", sensor=1))
    store.add_stats(stats(0, station="StationA", sensor=0))
    store.add_stats(stats(0, station="StationB", sensor=2))
    assert store.stations() == ["StationA", "StationB"]
    assert store.sensors("StationA") == [0, 1]
