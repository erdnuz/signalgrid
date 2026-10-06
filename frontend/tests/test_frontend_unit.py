import pathlib
import sys
import importlib.util
from datetime import datetime, timezone


import os
os.environ.setdefault("NATS_URL", "nats://test:4222")
MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("frontend_main", MODULE_PATH)
frontend_main = importlib.util.module_from_spec(SPEC)
sys.modules["frontend_main"] = frontend_main
SPEC.loader.exec_module(frontend_main)


def setup_function():
    frontend_main.data_store.clear()


def test_get_station_options_includes_any_and_known_stations():
    frontend_main.data_store[("StationB", 1)]
    frontend_main.data_store[("StationA", 0)]

    options = frontend_main.get_station_options()

    assert options[0] == {"label": "Any", "value": "Any"}
    assert {"label": "StationA", "value": "StationA"} in options
    assert {"label": "StationB", "value": "StationB"} in options


def test_apply_stats_event_trims_points():
    key = ("StationA", 0)
    for idx in range(frontend_main.MAX_POINTS + 10):
        frontend_main.apply_stats_event(
            {
                "station": key[0],
                "sensor": key[1],
                "timestamp": 1700000000000 + idx,
                "mean": float(idx),
            }
        )

    entry = frontend_main.data_store[key]
    assert len(entry["timestamps"]) == frontend_main.MAX_POINTS
    assert len(entry["actuals"]) == frontend_main.MAX_POINTS


def test_apply_forecast_event_handles_short_existing_buffer():
    key = ("StationA", 0)
    entry = frontend_main.data_store[key]
    entry["forecast_ts"] = [datetime.now(tz=timezone.utc)]
    entry["forecasts"] = [1.0]
    entry["lower_ci"] = [0.5]
    entry["upper_ci"] = [1.5]

    base = datetime.now(tz=timezone.utc)
    frontend_main.apply_forecast_event(
        {
            "station": key[0],
            "sensor": key[1],
            "timestamps": [
                base.isoformat(),
                base.isoformat(),
                base.isoformat(),
            ],
            "forecasts": [2.0, 2.1, 2.2],
            "lower_ci": [1.8, 1.9, 2.0],
            "upper_ci": [2.2, 2.3, 2.4],
        }
    )

    entry = frontend_main.data_store[key]
    assert len(entry["forecast_ts"]) == 3
    assert entry["forecasts"] == [2.0, 2.1, 2.2]


def _add_series(station, sensor, n=5):
    for idx in range(n):
        frontend_main.apply_stats_event(
            {"station": station, "sensor": sensor, "timestamp": 1700000000000 + idx, "mean": float(idx)}
        )


def test_update_graph_adds_one_actual_trace_per_matching_series():
    # Regression: a duplicated nested loop used to add N^2 traces.
    _add_series("StationA", 0)
    _add_series("StationB", 0)
    fig = frontend_main.update_graph(0, "Any", 0)
    actual_traces = [t for t in fig.data if t.name and t.name.endswith("Actual")]
    assert len(actual_traces) == 2


def test_update_graph_applies_dark_layout_without_forecasts():
    # Regression: layout was only applied once a forecast existed.
    _add_series("StationA", 0)
    fig = frontend_main.update_graph(0, "Any", 0)
    assert fig.layout.paper_bgcolor == "#181c20"


def test_choose_sensor_keeps_valid_selection_and_falls_back():
    options = [{"label": 0, "value": 0}, {"label": 1, "value": 1}]
    assert frontend_main.choose_sensor(options, 1) == 1
    assert frontend_main.choose_sensor(options, 7) == 0
    assert frontend_main.choose_sensor([], None) is None
