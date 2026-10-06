#!/usr/bin/env python3
"""End-to-end smoke test against a running `docker compose` stack.

1. Waits for the archive to report ready and for windows from every station
   and sensor to land in Postgres.
2. Stops the archive for a while, restarts it, and verifies the archived
   series has no gaps and no duplicates. This exercises the JetStream durable
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

    for st in STATIONS:
        for s in SENSORS:
            start = last_before[(st, s)] - 5_000
            rows = [r for r in series(st, s) if r["timestamp"] >= start]
            ts = [r["timestamp"] for r in rows]
            assert len(ts) == len(set(ts)), f"duplicate windows for {st}/{s}"
            gaps = [(a, b) for a, b in zip(ts, ts[1:], strict=False) if b - a != rows[0]["window_ms"]]
            if gaps:
                sys.exit(f"FAIL  {st}/{s}: {len(gaps)} gaps, first {gaps[0]}")
    print(f"ok    no gaps or duplicates across the outage in {len(STATIONS) * len(SENSORS)} series")

    wait_for(
        "dashboard healthy and receiving forecasts",
        lambda: (h := get_json(f"{FRONTEND}/health"))["nats_connected"] and h.get("forecasts", 0) > 0,
    )
    print("PASS")


if __name__ == "__main__":
    main()
