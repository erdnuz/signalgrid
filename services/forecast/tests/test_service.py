import numpy as np

from signalgrid_forecast.config import Settings
from signalgrid_forecast.models import Stats
from signalgrid_forecast.ou import MIN_POINTS
from signalgrid_forecast.service import ForecastService


def make_stats(ts: int, mean: float, sensor: int = 0) -> Stats:
    return Stats(
        station="StationA",
        sensor=sensor,
        timestamp=ts,
        window_ms=500,
        mean=mean,
        min=mean - 1,
        max=mean + 1,
        count=5,
    )


def ar1(n: int, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    x = [0.0]
    for _ in range(n - 1):
        x.append(0.8 * x[-1] + rng.standard_normal() * 0.1)
    return x


def service() -> ForecastService:
    return ForecastService(Settings(nats_url="nats://unused:4222"))


def test_no_forecast_until_enough_history():
    svc = service()
    outputs = [svc.handle_stats(make_stats(i * 500, v)) for i, v in enumerate(ar1(MIN_POINTS - 1))]
    assert all(o is None for o in outputs)


def test_forecast_timestamps_step_by_window():
    svc = service()
    out = None
    for i, v in enumerate(ar1(40)):
        out = svc.handle_stats(make_stats(i * 500, v))
    assert out is not None
    assert out.origin_ts == 39 * 500
    assert out.timestamps == [40 * 500, 41 * 500, 42 * 500]
    assert out.confidence == 0.98
    assert all(lo <= f <= hi for lo, f, hi in zip(out.lower_ci, out.forecasts, out.upper_ci, strict=True))


def test_duplicate_and_out_of_order_windows_are_ignored():
    svc = service()
    for i, v in enumerate(ar1(30)):
        svc.handle_stats(make_stats(i * 500, v))
    assert svc.handle_stats(make_stats(29 * 500, 99.0)) is None
    assert svc.handle_stats(make_stats(10 * 500, 99.0)) is None


def test_forecasts_are_scored_online():
    svc = service()
    out = None
    for i, v in enumerate(ar1(120)):
        out = svc.handle_stats(make_stats(i * 500, v))
    assert out is not None
    # Every window after the first forecast was scored against its prediction.
    assert out.metrics.n == 120 - MIN_POINTS
    assert out.metrics.coverage is not None and out.metrics.coverage > 0.85


def test_series_are_independent_per_sensor():
    svc = service()
    for i, v in enumerate(ar1(30)):
        svc.handle_stats(make_stats(i * 500, v, sensor=0))
    assert svc.handle_stats(make_stats(30 * 500, 0.0, sensor=1)) is None
