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

FORECAST_HORIZON = 3
TIME_DELTA_SEC = 0.5  # matches forge WINDOW_MS
MAX_POINTS = 50
MIN_POINTS = 20
CI_ALPHA = 0.02  # two-sided -> 98% interval
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

def forecast_ou(values, n_steps=3, alpha=CI_ALPHA):
    """Fit a discrete OU / AR(1) model x_{t+1} = c + phi * x_t + eps by OLS and
    return h-step forecasts with (1 - alpha) prediction intervals.

    Interval variance = innovation variance accumulated over h steps
    + parameter uncertainty propagated by the delta method:
        d xhat_h / d phi = h * phi^(h-1) * (x_T - mu)
        d xhat_h / d mu  = 1 - phi^h
    (the phi/mu covariance is ignored).
    """
    if len(values) < MIN_POINTS:
        return None, None

    x = np.asarray(values, dtype=float)
    x_t, x_tp1 = x[:-1], x[1:]
    n = len(x_t)

    sxx = np.sum((x_t - x_t.mean()) ** 2)
    if sxx == 0:
        return None, None

    phi = np.sum((x_t - x_t.mean()) * (x_tp1 - x_tp1.mean())) / sxx
    if not np.isfinite(phi) or np.isclose(phi, 1.0) or np.isclose(phi ** 2, 1.0):
        return None, None

    c = x_tp1.mean() - phi * x_t.mean()
    mu = c / (1 - phi)
    resid = x_tp1 - (c + phi * x_t)
    sigma2 = np.sum(resid ** 2) / (n - 2)
    if not np.isfinite(sigma2):
        return None, None

    se_phi = np.sqrt(sigma2 / sxx)
    se_mu = np.sqrt(sigma2 / n) / abs(1 - phi)
    t_val = t.ppf(1 - alpha / 2, df=n - 2)

    last_val = x[-1]
    forecasts, ci_list = [], []
    for h in range(1, n_steps + 1):
        point = mu + phi ** h * (last_val - mu)
        innovation_var = sigma2 * (1 - phi ** (2 * h)) / (1 - phi ** 2)
        param_var = (h * phi ** (h - 1) * (last_val - mu) * se_phi) ** 2 + ((1 - phi ** h) * se_mu) ** 2
        var_h = innovation_var + param_var
        if not np.isfinite(var_h) or var_h < 0:
            return None, None

        half_width = t_val * np.sqrt(var_h)
        forecasts.append(float(point))
        ci_list.append((float(point - half_width), float(point + half_width)))

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
                await nc.publish(f"sg.forecasts.{key[0]}.{key[1]}", json.dumps(forecast_msg).encode())
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


async def process_stats_and_forecast(nats_url: str, stop_event: asyncio.Event):
    backoff_sec = INITIAL_RETRY_BACKOFF_SEC

    while not stop_event.is_set():
        nc = NATS()

        try:
            await nc.connect(servers=[nats_url])
            logger.info("service=forecast event=nats_connected nats_url=%s", nats_url)
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

            await nc.subscribe("sg.stats.>", cb=handle_stats)
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

    nats_url = os.environ["NATS_URL"]  # required; fail fast if missing
    await process_stats_and_forecast(nats_url, stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("service=forecast event=keyboard_interrupt")
