import pathlib
import sys
import importlib.util


import os
os.environ.setdefault("NATS_URL", "nats://test:4222")
MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("frontend_main", MODULE_PATH)
frontend_main = importlib.util.module_from_spec(SPEC)
sys.modules["frontend_main"] = frontend_main
SPEC.loader.exec_module(frontend_main)


def test_health_endpoint_returns_ok_status():
    client = frontend_main.app.server.test_client()
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "frontend"


def test_sensor_options_filter_by_station():
    frontend_main.data_store.clear()
    frontend_main.data_store[("StationA", 0)]
    frontend_main.data_store[("StationA", 1)]
    frontend_main.data_store[("StationB", 2)]

    options = frontend_main.get_sensor_options("StationA")
    values = [item["value"] for item in options]

    assert values == [0, 1]
