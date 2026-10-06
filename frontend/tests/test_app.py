from signalgrid_frontend.app import choose, create_app, kpi_values
from signalgrid_frontend.config import Settings
from signalgrid_frontend.store import Kpis


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


def test_kpi_formatting_handles_missing_values():
    k = Kpis(
        mae=0.1234,
        coverage=0.975,
        confidence=0.98,
        regime_accuracy=None,
        latency_ms_p50=42.4,
        windows_per_s=16.0,
    )
    assert kpi_values(k) == ["0.123", "97.5%", "—", "42 ms", "16.0"]
