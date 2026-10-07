#!/usr/bin/env python3
"""End-to-end smoke test against a running `docker compose` stack.

1. Waits for the archive to report ready and for windows from every station
   and sensor to land in Postgres.
2. Stops the archive for a while, restarts it, and verifies the archive
   holds exactly the windows published to JetStream (none lost or duplicated).
   This exercises the JetStream durable
   consumer + idempotent insert path (effectively-once delivery).
3. Checks the dashboard is healthy and has received forecasts.

Standard library only, so CI can run it without installing anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from itertools import pairwise

ARCHIVE = os.environ.get("ARCHIVE_URL", "http://localhost:8003")
FRONTEND = os.environ.get("FRONTEND_URL", "http://localhost:8004")
STATIONS = [f"Station{chr(ord('A') + i)}" for i in range(int(os.environ.get("N_STATIONS", "2")))]
SENSORS = range(4)
OUTAGE_S = float(os.environ.get("OUTAGE_S", "8"))


def get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def wait_for(description: str, predicate, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                print(f"ok    {description}")
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError) as exc:
            last_error = exc
        time.sleep(1)
    sys.exit(f"FAIL  {description} (timed out; last error: {last_error})")


def series(station: str, sensor: int) -> list[dict]:
    rows = get_json(f"{ARCHIVE}/stats?station={station}&sensor={sensor}&limit=10000")
    assert isinstance(rows, list)
    return rows


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


JS_DUMP = r"""
import asyncio, datetime, json, sys
import nats
from nats.js.api import ConsumerConfig, DeliverPolicy

async def main():
    nc = await nats.connect("nats://nats:4222")
    js = nc.jetstream()
    since = int(sys.argv[1])
    start = datetime.datetime.fromtimestamp(since / 1000, tz=datetime.timezone.utc).isoformat()
    sub = await js.pull_subscribe(
        "sg.stats.>",
        config=ConsumerConfig(deliver_policy=DeliverPolicy.BY_START_TIME, opt_start_time=start),
    )
    out = {}
    while True:
        try:
            batch = await sub.fetch(1000, timeout=2)
        except Exception:
            break  # caught up
        for msg in batch:
            d = json.loads(msg.data)
            if d["timestamp"] >= since:
                out.setdefault(f'{d["station"]}|{d["sensor"]}', []).append(d["timestamp"])
    print(json.dumps(out))
    await nc.close()

asyncio.run(main())
"""


def jetstream_windows(since_ms: int) -> dict[tuple[str, int], set[int]]:
    """Window starts per series stored in the SG_STATS stream (read from
    inside the compose network via the forecast container's NATS client)."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "forecast", "python", "-c", JS_DUMP, str(since_ms)],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(result.stdout.strip().splitlines()[-1])
    return {(k.split("|")[0], int(k.split("|")[1])): set(v) for k, v in raw.items()}


def main() -> None:
    wait_for("archive ready", lambda: get_json(f"{ARCHIVE}/ready")["status"] == "ready")
    wait_for(
        "windows archived for every station and sensor",
        lambda: all(len(series(st, s)) >= 10 for st in STATIONS for s in SENSORS),
    )

    # Only the segment around our own outage is checked, so gaps from earlier
    # producer restarts (real missing data, not lost messages) don't count.
    last_before = {(st, s): series(st, s)[-1]["timestamp"] for st in STATIONS for s in SENSORS}
    print(f"..    stopping archive for {OUTAGE_S:.0f}s")
    started = time.monotonic()
    compose("stop", "archive")
    stop_s = time.monotonic() - started
    if stop_s > 5:
        sys.exit(f"FAIL  archive took {stop_s:.1f}s to stop (SIGTERM not honoured?)")
    print(f"ok    archive stopped gracefully in {stop_s:.1f}s")
    time.sleep(OUTAGE_S)
    compose("start", "archive")
    wait_for("archive ready after restart", lambda: get_json(f"{ARCHIVE}/ready")["status"] == "ready")

    # Windows produced during the outage are redelivered from JetStream.
    outage_ms = int(OUTAGE_S * 1000)
    wait_for(
        "outage backlog redelivered",
        lambda: all(
            series(st, s)[-1]["timestamp"] >= last_before[(st, s)] + outage_ms for st in STATIONS for s in SENSORS
        ),
        timeout_s=60,
    )

    # Source of truth: the windows forge actually published to JetStream.
    # The archive must hold exactly those (none lost, none duplicated). Gaps
    # where the *producer* emitted nothing (e.g. a stalled CI runner) are not
    # delivery failures, so they are reported but do not fail the test.
    since = min(last_before.values()) - 5_000
    published = jetstream_windows(since)
    lost, dupes, data_gaps = 0, 0, 0
    for st in STATIONS:
        for s in SENSORS:
            rows = [r for r in series(st, s) if r["timestamp"] >= since]
            ts = [r["timestamp"] for r in rows]
            dupes += len(ts) - len(set(ts))
            stored = set(ts)
            horizon = max(ts) if ts else since
            expected = {t for t in published.get((st, s), set()) if t <= horizon}
            missing = sorted(expected - stored)
            if missing:
                print(f"FAIL  {st}/{s}: {len(missing)} published windows missing, first {missing[0]}")
            lost += len(missing)
            data_gaps += sum(1 for a, b in pairwise(ts) if b - a != rows[0]["window_ms"])
    if lost or dupes:
        sys.exit(f"FAIL  {lost} published windows lost, {dupes} duplicated")
    note = f" ({data_gaps} producer gaps, not delivery losses)" if data_gaps else ""
    print(
        f"ok    archive matches JetStream exactly across the outage: "
        f"{sum(len(v) for v in published.values())} windows, none lost or duplicated{note}"
    )

    wait_for(
        "dashboard healthy and receiving forecasts",
        lambda: (h := get_json(f"{FRONTEND}/health"))["nats_connected"] and h.get("forecasts", 0) > 0,
    )
    print("PASS")


if __name__ == "__main__":
    main()
