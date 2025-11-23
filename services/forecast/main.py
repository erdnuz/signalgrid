import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from scipy.stats import t
from nats.aio.client import Client as NATS
import numpy as np

NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")
FORECAST_HORIZON = 3
TIME_DELTA_SEC = 0.4
MAX_POINTS = 50
MIN_POINTS = 20

recent_data = defaultdict(list)
forecast_tasks = {}  # key -> asyncio.Task

def forecast_ou(values, n_steps=3, alpha=0.02):
    if len(values) < MIN_POINTS:
        return None, None

    x = np.array(values)
    x_t = x[:-1]
    x_tp1 = x[1:]

    # Fit discrete OU (AR1)
    phi = np.corrcoef(x_t, x_tp1)[0,1] * np.std(x_tp1)/np.std(x_t)
    mu = np.mean(x_tp1 - phi * x_t) / (1 - phi)
    sigma_eps = np.sqrt(np.mean((x_tp1 - (phi*x_t + (1-phi)*mu))**2))

    last_val = x[-1]
    forecasts = []
    ci_list = []

    n = len(values)
    t_val = t.ppf(1 - alpha/2, df=n-2)  # df = n-2 for slope/intercept

    for h in range(1, n_steps+1):
        next_val = mu + phi**h * (last_val - mu)
        forecasts.append(next_val)
        se_phi = sigma_eps / np.sqrt(np.sum((x_t - np.mean(x_t))**2))
        se_mu = sigma_eps * np.sqrt(1/len(x_t) + np.mean(x_t)**2 / np.sum((x_t - np.mean(x_t))**2))

        var_h = sigma_eps**2 * (1 - phi**(2*h)) / (1 - phi**2)
        var_h += (h * se_phi)**2 + se_mu**2  # approximate accumulation of parameter uncertainty

        lower = next_val - t_val * np.sqrt(var_h)
        upper = next_val + t_val * np.sqrt(var_h)
        ci_list.append((lower, upper))

    return forecasts, ci_list


async def forecast_loop(nc: NATS, key):
    """Run continuous forecasting for a single channel."""
    while True:
        if key not in recent_data or not recent_data[key]:
            await asyncio.sleep(TIME_DELTA_SEC)
            continue

        ts, val = recent_data[key][-1]
        values = [v for _, v in recent_data[key]]

        forecast_ts_list = [ts + timedelta(seconds=TIME_DELTA_SEC * i) for i in range(1, FORECAST_HORIZON + 1)]
        forecast_vals_list, ci_list = forecast_ou(values, n_steps=FORECAST_HORIZON)

        if forecast_vals_list:
            station, sensor = key
            forecast_msg = {
                "station": station,
                "sensor": sensor,
                "timestamps": [t.isoformat() for t in forecast_ts_list],
                "forecasts": forecast_vals_list,
                "upper_ci": [c[1] for c in ci_list],
                "lower_ci": [c[0] for c in ci_list]
            }
            await nc.publish("forecasts", json.dumps(forecast_msg).encode())
        await asyncio.sleep(TIME_DELTA_SEC)  # wait until next forecast step

async def process_stats_and_forecast():
    nc = NATS()
    await nc.connect(servers=[NATS_URL])

    async def handle_stats(msg):
        stat = json.loads(msg.data.decode())
        station = stat["station"]
        sensor = stat["sensor"]
        ts_millis = stat["timestamp"]
        ts = datetime.fromtimestamp(ts_millis / 1000, tz=timezone.utc)
        mean_val = stat["mean"]

        key = (station, sensor)
        recent_data[key].append((ts, mean_val))
        if len(recent_data[key]) > MAX_POINTS:
            recent_data[key] = recent_data[key][-MAX_POINTS:]

        # Launch forecast task if not already running
        if key not in forecast_tasks:
            forecast_tasks[key] = asyncio.create_task(forecast_loop(nc, key))

    await nc.subscribe("stats", cb=handle_stats)
    print("Subscribed to stats and launching per-channel forecasts...")

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == "__main__":
    try:
        asyncio.run(process_stats_and_forecast())
    except KeyboardInterrupt:
        print("Exiting...")
