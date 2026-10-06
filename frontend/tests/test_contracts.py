from signalgrid_frontend.models import ForecastMessage, RegimeMessage, Stats


def test_dashboard_parses_every_contract(contract):
    Stats.model_validate(contract("stats.json"))
    ForecastMessage.model_validate(contract("forecast.json"))
    RegimeMessage.model_validate(contract("regime.json"))
