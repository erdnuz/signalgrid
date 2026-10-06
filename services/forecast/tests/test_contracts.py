"""The Rust producers are tested against the same golden files, so these
tests fail if either side changes the wire format."""

from signalgrid_forecast.models import ForecastMessage, RawEvent, RegimeMessage, Stats


def test_stats_contract(contract):
    stats = Stats.model_validate(contract("stats.json"))
    assert stats.window_ms == 500 and stats.regime == 1


def test_raw_event_contract(contract):
    raw = RawEvent.model_validate(contract("raw_event.json"))
    assert len(raw.values) == 4


def test_forecast_contract_round_trips(contract):
    payload = contract("forecast.json")
    assert ForecastMessage.model_validate(payload).model_dump(mode="json") == payload


def test_regime_contract_round_trips(contract):
    payload = contract("regime.json")
    assert RegimeMessage.model_validate(payload).model_dump(mode="json") == payload
