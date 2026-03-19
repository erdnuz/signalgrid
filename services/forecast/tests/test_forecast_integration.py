import asyncio
import json
import pathlib
import sys
import importlib.util
from datetime import datetime, timezone

import pytest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("forecast_main", MODULE_PATH)
forecast_main = importlib.util.module_from_spec(SPEC)
sys.modules["forecast_main"] = forecast_main
SPEC.loader.exec_module(forecast_main)


class FakeNATS:
    def __init__(self):
        self.published = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


@pytest.mark.asyncio
async def test_forecast_loop_publishes_forecast_message():
    forecast_main.recent_data.clear()
    key = ("StationA", 0)
    now = datetime.now(tz=timezone.utc)

    for idx in range(forecast_main.MIN_POINTS + 3):
        forecast_main.recent_data[key].append((now, 20.0 + idx * 0.2))

    fake_nc = FakeNATS()
    stop_event = asyncio.Event()

    original_forecast_ou = forecast_main.forecast_ou
    forecast_main.forecast_ou = lambda values, n_steps=3, alpha=0.02: (
        [1.0] * n_steps,
        [(0.5, 1.5)] * n_steps,
    )

    async def stop_soon():
        await asyncio.sleep(forecast_main.TIME_DELTA_SEC * 1.5)
        stop_event.set()

    try:
        await asyncio.gather(
            forecast_main.forecast_loop(fake_nc, key, stop_event),
            stop_soon(),
        )
    finally:
        forecast_main.forecast_ou = original_forecast_ou

    assert len(fake_nc.published) >= 1
    subject, payload = fake_nc.published[0]
    assert subject == "forecasts"

    message = json.loads(payload.decode())
    assert message["station"] == "StationA"
    assert message["sensor"] == 0
    assert len(message["forecasts"]) == forecast_main.FORECAST_HORIZON
