import asyncio
import json
import logging
import os
import signal
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from scipy.stats import t
from nats.aio.client import Client as NATS
import numpy as np

NATS_URL = os.environ["NATS_URL"]  # Required, fail if missing
FORECAST_HORIZON = 3
TIME_DELTA_SEC = 0.4
MAX_POINTS = 50
MIN_POINTS = 20
INITIAL_RETRY_BACKOFF_SEC = 1
MAX_RETRY_BACKOFF_SEC = 30

recent_data = defaultdict(list)
forecast_tasks = {}  # key -> asyncio.Task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("forecast")


def parse_stats_event(stat):
    station = stat["station"]
    sensor = stat["sensor"]
    ts_millis = stat["timestamp"]
    ts = datetime.fromtimestamp(ts_millis / 1000, tz=timezone.utc)
    mean_val = stat["mean"]
    return (station, sensor), ts, mean_val


def update_recent_series(key, ts, mean_val):
    recent_data[key].append((ts, mean_val))
    if len(recent_data[key]) > MAX_POINTS:
        recent_data[key] = recent_data[key][-MAX_POINTS:]


def build_forecast_message(key, forecast_ts_list, forecast_vals_list, ci_list):
    station, sensor = key
    return {
        "station": station,
        "sensor": sensor,
        "timestamps": [t.isoformat() for t in forecast_ts_list],
        "forecasts": forecast_vals_list,
        "upper_ci": [c[1] for c in ci_list],
        "lower_ci": [c[0] for c in ci_list],
    }


def ensure_forecast_task(nc, key, stop_event):
    if key not in forecast_tasks or forecast_tasks[key].done():
        forecast_tasks[key] = asyncio.create_task(
            forecast_loop(nc, key, stop_event)
        )

def forecast_ou(values, n_steps=3, alpha=0.02):
    if len(values) < MIN_POINTS:
        return None, None

    x = np.array(values)
    x_t = x[:-1]
    x_tp1 = x[1:]

    if np.std(x_t) == 0 or np.sum((x_t - np.mean(x_t)) ** 2) == 0:
        return None, None

    # Fit discrete OU (AR1)
    phi = np.corrcoef(x_t, x_tp1)[0, 1] * np.std(x_tp1) / np.std(x_t)

    if np.isnan(phi) or np.isinf(phi):
        return None, None

    mu = np.mean(x_tp1 - phi * x_t) / (1 - phi)
    sigma_eps = np.sqrt(np.mean((x_tp1 - (phi * x_t + (1 - phi) * mu)) ** 2))

    if np.isnan(sigma_eps) or np.isinf(sigma_eps):
        return None, None

    last_val = x[-1]
    forecasts = []
    ci_list = []

    n = len(values)
    t_val = t.ppf(1 - alpha / 2, df=n - 2)  # df = n-2 for slope/intercept

    if np.isnan(t_val) or np.isinf(t_val):
        return None, None

    for h in range(1, n_steps + 1):
        next_val = mu + phi ** h * (last_val - mu)
        forecasts.append(next_val)
        denom = np.sum((x_t - np.mean(x_t)) ** 2)
        if denom == 0:
            return None, None

        se_phi = sigma_eps / np.sqrt(denom)
        se_mu = sigma_eps * np.sqrt(1 / len(x_t) + np.mean(x_t) ** 2 / denom)

        if np.isclose(1 - phi ** 2, 0.0):
            return None, None

        var_h = sigma_eps ** 2 * (1 - phi ** (2 * h)) / (1 - phi ** 2)
        var_h += (h * se_phi)**2 + se_mu**2  # approximate accumulation of parameter uncertainty

        if var_h < 0 or np.isnan(var_h) or np.isinf(var_h):
            return None, None

        lower = next_val - t_val * np.sqrt(var_h)
        upper = next_val + t_val * np.sqrt(var_h)
        ci_list.append((lower, upper))

    return forecasts, ci_list


async def forecast_loop(nc: NATS, key, stop_event: asyncio.Event):
    """Run continuous forecasting for a single channel."""
    try:
        while not stop_event.is_set():
            if key not in recent_data or not recent_data[key]:
                await asyncio.sleep(TIME_DELTA_SEC)
                continue

            ts, _ = recent_data[key][-1]
            values = [v for _, v in recent_data[key]]

            forecast_ts_list = [
                ts + timedelta(seconds=TIME_DELTA_SEC * i)
                for i in range(1, FORECAST_HORIZON + 1)
            ]
            forecast_vals_list, ci_list = forecast_ou(values, n_steps=FORECAST_HORIZON)

            if forecast_vals_list:
                forecast_msg = build_forecast_message(
                    key,
                    forecast_ts_list,
                    forecast_vals_list,
                    ci_list,
                )
                await nc.publish("forecasts", json.dumps(forecast_msg).encode())
            await asyncio.sleep(TIME_DELTA_SEC)
    except asyncio.CancelledError:
        logger.info("service=forecast event=task_cancelled key=%s", key)
        raise


async def stop_forecast_tasks():
    tasks = list(forecast_tasks.values())
    forecast_tasks.clear()

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def process_stats_and_forecast(stop_event: asyncio.Event):
    backoff_sec = INITIAL_RETRY_BACKOFF_SEC

    while not stop_event.is_set():
        nc = NATS()

        try:
            await nc.connect(servers=[NATS_URL])
            logger.info("service=forecast event=nats_connected nats_url=%s", NATS_URL)
            backoff_sec = INITIAL_RETRY_BACKOFF_SEC

            async def handle_stats(msg):
                try:
                    stat = json.loads(msg.data.decode())
                    key, ts, mean_val = parse_stats_event(stat)
                    update_recent_series(key, ts, mean_val)
                    ensure_forecast_task(nc, key, stop_event)
                except Exception as exc:
                    logger.exception(
                        "service=forecast event=handle_stats_failed error=%s", exc
                    )

            await nc.subscribe("stats", cb=handle_stats)
            logger.info("service=forecast event=subscribed topic=stats")

            await stop_event.wait()
        except Exception as exc:
            logger.error(
                "service=forecast event=nats_connect_failed backoff_sec=%s error=%s",
                backoff_sec,
                exc,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff_sec)
            except asyncio.TimeoutError:
                pass
            backoff_sec = min(backoff_sec * 2, MAX_RETRY_BACKOFF_SEC)
        finally:
            await stop_forecast_tasks()
            if nc.is_connected:
                await nc.drain()
                logger.info("service=forecast event=nats_drained")


async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await process_stats_and_forecast(stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("service=forecast event=keyboard_interrupt")
