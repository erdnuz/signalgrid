import pathlib
import sys
import importlib.util
from datetime import datetime, timezone

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("forecast_main", MODULE_PATH)
forecast_main = importlib.util.module_from_spec(SPEC)
sys.modules["forecast_main"] = forecast_main
SPEC.loader.exec_module(forecast_main)


def test_parse_stats_event_extracts_key_and_values():
    stat = {
        "station": "StationA",
        "sensor": 3,
        "timestamp": 1700000000000,
        "mean": 12.5,
    }

    key, ts, mean_val = forecast_main.parse_stats_event(stat)

    assert key == ("StationA", 3)
    assert ts == datetime.fromtimestamp(1700000000000 / 1000, tz=timezone.utc)
    assert mean_val == 12.5


def test_update_recent_series_trims_to_max_points():
    forecast_main.recent_data.clear()
    key = ("S", 1)

    for idx in range(forecast_main.MAX_POINTS + 5):
        forecast_main.update_recent_series(key, datetime.now(tz=timezone.utc), float(idx))

    assert len(forecast_main.recent_data[key]) == forecast_main.MAX_POINTS


def test_build_forecast_message_contains_expected_fields():
    key = ("StationX", 5)
    timestamps = [datetime(2024, 1, 1, tzinfo=timezone.utc)]
    forecasts = [10.0]
    ci = [(9.5, 10.5)]

    message = forecast_main.build_forecast_message(key, timestamps, forecasts, ci)

    assert message["station"] == "StationX"
    assert message["sensor"] == 5
    assert message["forecasts"] == [10.0]
    assert message["lower_ci"] == [9.5]
    assert message["upper_ci"] == [10.5]
