from signalgrid_frontend.models import ForecastMessage, ForecastMetrics, RegimeMessage, Stats


def stats(ts: int, mean: float = 1.0, station: str = "StationA", sensor: int = 0) -> Stats:
    return Stats(
        station=station,
        sensor=sensor,
        timestamp=ts,
        window_ms=500,
        mean=mean,
        min=mean - 1,
        max=mean + 1,
        count=5,
    )


def forecast(origin: int, station: str = "StationA", sensor: int = 0) -> ForecastMessage:
    ts = [origin + 500 * h for h in (1, 2, 3)]
    return ForecastMessage(
        station=station,
        sensor=sensor,
        origin_ts=origin,
        step_ms=500,
        timestamps=ts,
        forecasts=[1.0, 1.1, 1.2],
        lower_ci=[0.5, 0.5, 0.5],
        upper_ci=[1.5, 1.6, 1.7],
        confidence=0.98,
        metrics=ForecastMetrics(n=10, mae=0.1, coverage=0.97),
    )


def regime(ts: int, true: int, mapped: int, station: str = "StationA") -> RegimeMessage:
    return RegimeMessage(
        station=station,
        timestamp=ts,
        probabilities=[0.1, 0.8, 0.1],
        state=1,
        true_regime=true,
        mapped_state=mapped,
        accuracy=0.9,
        n_scored=100,
    )
