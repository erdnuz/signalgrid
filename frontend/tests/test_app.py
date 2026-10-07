from dash import no_update

from helpers import alert, forecast, stats
from signalgrid_frontend.app import (
    TIME_GRAPHS,
    choose,
    coerce_selection,
    create_app,
    kpi_values,
    refresh,
    resolve,
    slow_refresh,
)
from signalgrid_frontend.config import Settings
from signalgrid_frontend.store import Kpis

MAIN = TIME_GRAPHS.index("main-graph")


def test_health_endpoint(store):
    app = create_app(Settings(nats_url="nats://unused:4222"), store, health=lambda: True)
    resp = app.server.test_client().get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "status": "ok",
        "service": "frontend",
        "nats_connected": True,
        "series": 0,
        "forecasts": 0,
        "regime_stations": 0,
    }


def test_choose_keeps_valid_selection_and_falls_back():
    assert choose([0, 1], 1) == 1
    assert choose([0, 1], 7) == 0
    assert choose([], None) is None


def test_selection_is_coerced_between_single_and_multi_views():
    assert coerce_selection("single", ["B", "A"], ["A", "B"], default_all=False) == "B"
    assert coerce_selection("compare", "A", ["A", "B"], default_all=False) == ["A"]
    assert coerce_selection("aggregate", "A", ["A", "B"], default_all=True) == ["A", "B"]
    assert coerce_selection("compare", ["Z"], ["A", "B"], default_all=False) == ["A"]


def test_resolve_builds_the_station_sensor_product():
    keys = [("A", 0), ("A", 1), ("B", 0), ("B", 1)]
    assert resolve("single", "A", 1, keys) == ([("A", 1)], ["A"])
    assert resolve("compare", ["A", "B"], [0], keys) == ([("A", 0), ("B", 0)], ["A", "B"])


def test_kpi_formatting_handles_missing_values():
    k = Kpis(
        mase={1: 0.912, 3: None},
        coverage={1: 0.975, 3: 0.99},
        confidence=0.98,
        regime_accuracy=None,
        active_alerts=2,
        severe_alerts=1,
        latency_ms_p50=3.4,
        windows_per_s=16.0,
    )
    assert kpi_values(k) == ["0.91 · —", "97.5% · 99.0%", "—", "2", "3 ms", "16.0"]


def _fill(store, n, start=0, sensor=0):
    for i in range(start, start + n):
        store.add_stats(stats(i * 500, float(i), sensor=sensor), received_ms=i * 500 + 600)


def test_refresh_renders_once_then_only_appends(store):
    _fill(store, 5)
    now = 2_400
    figures, extends, cursor, _, _ = refresh(store, "single", "StationA", 0, "1m", "raw", None, now_ms=now)
    assert figures[MAIN] is not no_update  # first paint
    assert extends[MAIN] is no_update

    _fill(store, 2, start=5)
    figures, extends, cursor2, _, _ = refresh(store, "single", "StationA", 0, "1m", "raw", cursor, now_ms=now)
    assert figures[MAIN] is no_update  # no redraw
    update, indices, _ = extends[MAIN]
    actual = 6  # index of the actual-values series in the detail chart
    assert update["y"][indices.index(actual)] == [5.0, 6.0]

    # Nothing new (and the anchor has not moved a window): client untouched.
    figures, extends, cursor3, kpis, titles = refresh(
        store, "single", "StationA", 0, "1m", "raw", cursor2, now_ms=now
    )
    assert all(f is no_update for f in figures)
    assert all(e is no_update for e in extends)
    assert cursor3 is no_update
    assert all(k is no_update for k in kpis) and all(t is no_update for t in titles)


def test_refresh_redraws_on_view_range_or_backfill(store):
    _fill(store, 5, start=10)
    _fill(store, 5, start=10, sensor=1)
    _, _, cursor, _, _ = refresh(store, "single", "StationA", 0, "1m", "raw", None, now_ms=8_000)

    figures, _, cursor, _, _ = refresh(store, "single", "StationA", 0, "5m", "raw", cursor, now_ms=8_000)
    assert figures[MAIN] is not no_update

    figures, _, cursor, _, titles = refresh(
        store, "compare", ["StationA"], [0, 1], "5m", "raw", cursor, now_ms=8_000
    )
    assert figures[MAIN] is not no_update and titles[0] == "Comparing 2 series"

    store.backfill(("StationA", 0), [stats(0), stats(500)])
    figures, _, _, _, _ = refresh(store, "compare", ["StationA"], [0, 1], "5m", "raw", cursor, now_ms=8_000)
    assert figures[MAIN] is not no_update


def test_quality_panels_appear_once_forecasts_are_scored(store):
    _fill(store, 3)
    _, _, cursor, _, _ = refresh(store, "single", "StationA", 0, "1m", "raw", None, now_ms=1_500)
    store.add_forecast(forecast(1_000))
    figures, _, _, _, _ = refresh(store, "single", "StationA", 0, "1m", "raw", cursor, now_ms=1_500)
    for graph in ("mase1-graph", "mase3-graph", "cov1-graph", "cov3-graph"):
        assert figures[TIME_GRAPHS.index(graph)] is not no_update  # empty state -> chart


def test_slow_refresh_only_sends_changes(store):
    _fill(store, 30)
    store.add_alert(alert(ts=1_000))
    health, _corr, feed, note, digest = slow_refresh(
        store, "single", "StationA", 0, "1m", None, now_ms=15_000
    )
    assert health is not no_update and len(feed) == 1 and "1 active" in note
    again = slow_refresh(store, "single", "StationA", 0, "1m", digest, now_ms=15_000)
    assert all(x is no_update for x in again)
