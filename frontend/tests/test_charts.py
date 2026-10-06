from helpers import forecast, regime, stats
from signalgrid_frontend import charts
from signalgrid_frontend.charts import SURFACE, Chart, Series, Trace


def test_delta_sends_only_points_after_the_cursor():
    trace = Trace([0, 500, 1_000], [1.0, 2.0, 3.0])
    payload, last = charts.delta([trace], [500], range_ms=60_000)
    update, indices, caps = payload
    assert indices == [0]
    assert update == {"x": [[1_000]], "y": [[3.0]]}  # epoch ms, no date strings
    assert caps["x"] == [121]  # one range of points (60 s / 500 ms + 1)
    assert last == [1_000]


def test_delta_is_none_when_nothing_is_new():
    payload, last = charts.delta([Trace([0, 500], [1.0, 2.0])], [500], range_ms=60_000)
    assert payload is None
    assert last == [500]


def test_delta_applies_the_render_time_normalisation():
    payload, _ = charts.delta([Trace([0, 500], [1.0, 5.0])], [0], 60_000, affine=[[1.0, 2.0]])
    assert payload[0]["y"] == [[2.0]]


def test_replace_traces_resend_their_batch_capped_to_its_length():
    horizon = Trace([1_000, 1_500, 2_000], [1.0, 1.1, 1.2], replace=True)
    assert charts.delta([horizon], [1_000], 60_000)[0] is None  # same origin
    payload, last = charts.delta([horizon], [500], 60_000)
    assert len(payload[0]["y"][0]) == 3 and payload[2]["y"] == [3]
    assert last == [1_000]


def test_every_time_chart_ends_with_a_range_anchor():
    chart = Chart([Series(Trace([0, 500], [1.0, 2.0]), "a")])
    traces = charts.traces_of(chart, end_ms=60_000, range_ms=60_000)
    assert len(traces) == 2
    assert traces[-1].replace and list(traces[-1].x) == [0, 60_000]
    # The anchor moves with time, so the window slides with tiny updates.
    moved = charts.traces_of(chart, end_ms=60_500, range_ms=60_000)
    payload, _ = charts.delta(moved, charts.cursor_for(traces), 60_000)
    assert payload[1] == [1]


def test_decimate_bounds_points_and_keeps_band_edges_conservative():
    hi = Trace(list(range(3_000)), [float(i % 7) for i in range(3_000)], agg="max")
    out = charts.decimate(hi, max_n=300)
    assert len(out.x) <= 300
    assert max(out.y) == 6.0


def test_detail_chart_has_fixed_trace_layout(store):
    for i in range(5):
        store.add_stats(stats(i * 500))
    store.add_forecast(forecast(2_000))
    store.add_regime(regime(0, 0, 0))
    chart = charts.detail_chart(store.series("StationA", 0), store.regimes("StationA"))
    fig = charts.render_chart(chart, end_ms=3_000, range_ms=60_000, uirevision="rev")
    assert len(fig["data"]) == 10  # 9 series + anchor
    names = [t["name"] for t in fig["data"] if t["showlegend"]]
    assert names.count("Actual (window mean)") == 1
    assert {"Forecast (1 step ahead)", "98% interval", "True regime", "Detected regime"} <= set(names)
    assert fig["layout"]["paper_bgcolor"] == SURFACE
    assert fig["layout"]["xaxis2"]["type"] == "date"
    assert fig["layout"]["xaxis"]["matches"] == "x2"  # rows share the time axis


def test_compare_colours_follow_the_series_not_the_selection(store):
    for st in ("StationA", "StationB"):
        for s in (0, 1):
            store.add_stats(stats(0, station=st, sensor=s))
            store.add_stats(stats(500, station=st, sensor=s))
    keys = store.keys()
    only_b1 = charts.compare_chart([store.series("StationB", 1)], keys, False, 0)
    both = charts.compare_chart([store.series("StationA", 0), store.series("StationB", 1)], keys, False, 0)
    assert only_b1.series[0].color == both.series[1].color


def test_aggregate_chart_builds_mean_and_envelope(store):
    store.add_stats(stats(0, mean=1.0, sensor=0))
    store.add_stats(stats(0, mean=3.0, sensor=1))
    views = [store.series("StationA", 0), store.series("StationA", 1)]
    chart = charts.aggregate_chart({"StationA": views}, ["StationA"])
    hi, lo, mean = (s.trace.y for s in chart.series)
    assert (list(hi), list(lo), list(mean)) == ([3.0], [1.0], [2.0])


def test_correlation_uses_changes_and_is_symmetric(store):
    for i in range(40):
        store.add_stats(stats(i * 500, mean=float(i % 5), sensor=0))
        store.add_stats(stats(i * 500, mean=float(i % 5) * 2, sensor=1))
        store.add_stats(stats(i * 500, mean=float(-(i % 5)), sensor=2))
    views = [store.series("StationA", s) for s in (0, 1, 2)]
    keys, m = charts.correlation(views, cutoff_ms=0)
    assert m[0][1] == 1.0 and m[0][2] == -1.0 and m[1][2] == m[2][1]
    fig = charts.correlation_figure(keys, m)
    assert fig["data"][0]["zmin"] == -1 and fig["data"][0]["zmax"] == 1


def test_empty_states():
    fig = charts.render_chart(Chart([], empty="none yet"), 0, 60_000, "r")
    assert fig["layout"]["annotations"][0]["text"] == "none yet"


def test_dense_traces_are_decimated_and_drawn_with_webgl():
    n = 5_000  # e.g. a faster sampling rate than the default 500 ms windows
    trace = Trace(list(range(0, n * 100, 100)), [0.0] * n, interval_ms=100)
    fig = charts.render_chart(Chart([Series(trace, "x")]), n * 100, n * 100, "r")
    assert fig["data"][0]["type"] == "scattergl"
    assert len(fig["data"][0]["x"]) <= charts.MAX_RENDER_POINTS


def test_reference_line_and_label_are_drawn():
    chart = charts.metric_chart(
        [("x", "#fff", Trace([0, 500], [0.9, 1.1]))],
        y_title="MASE",
        y_format=".2f",
        empty="",
        include=[1.0],
        reference=(1.0, "persistence"),
    )
    fig = charts.render_chart(chart, 1_000, 60_000, "r")
    assert fig["layout"]["shapes"][0]["y0"] == 1.0
    assert fig["layout"]["annotations"][0]["text"] == "persistence"
    assert fig["layout"]["yaxis"]["autorangeoptions"] == {"include": [1.0]}
