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
