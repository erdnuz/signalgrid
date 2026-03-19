import asyncio
import json
import structlog
import os
import signal
from collections import defaultdict
from threading import Lock, Thread
from datetime import datetime

from dash import Dash, dcc, html
from dash.dependencies import Input, Output
from nats.aio.client import Client as NATS
import plotly.graph_objects as go

# -------------------------
# Configuration
# -------------------------

# --- Configurable constants ---
NATS_URL = os.environ["NATS_URL"]  # Required, fail if missing
MAX_POINTS = 50  # points to store per line
INTERVAL_MS = 1000  # ms between UI updates
INITIAL_RETRY_BACKOFF_SEC = 1
MAX_RETRY_BACKOFF_SEC = 30

# --- Structured logging setup ---
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger("frontend")

data_lock = Lock()

app = Dash(__name__, external_stylesheets=["/assets/minimal.css"])


@app.server.get("/health")
def health():
    return {"status": "ok", "service": "frontend"}, 200


app.layout = html.Div([
    html.H1("Live Sensor Forecast", className="title"),
    html.Div([
        html.Div([
            html.Label("Select Station", className="label"),
            dcc.Dropdown(
                id="station-dropdown",
                options=[{"label": "Any", "value": "Any"}],
                value="Any",
                clearable=False,
                className="dropdown",
                searchable=False
            )
        ], className="card"),
        html.Div([
            html.Label("Select Sensor", className="label"),
            dcc.Dropdown(
                id="sensor-dropdown",
                options=[],
                value=None,
                clearable=False,
                className="dropdown",
                searchable=False
            )
        ], className="card"),
    ], className="flex-row"),
    html.Div([
        dcc.Graph(
            id="live-graph",
            className="chart-container",
            style={"height": "700px", "width": "100%"},
            config={"displayModeBar": False, "scrollZoom": False}
        )
    ]),
    dcc.Interval(id="interval", interval=INTERVAL_MS),
], className="container")


# -------------------------
# Storage for live plotting
# key: (station, sensor) -> dict of lists
data_store = defaultdict(lambda: {
    "timestamps": [],        # actual event timestamps
    "actuals": [],           # actual values
    "forecast_ts": [],       # forecast timestamps
    "forecasts": [],
    "lower_ci":[],
    "upper_ci":[]        # forecasted values
})


def get_station_options():
    with data_lock:
        stations = sorted({station for station, _ in data_store.keys()})
    return [{"label": "Any", "value": "Any"}] + [
        {"label": station, "value": station} for station in stations
    ]


def get_sensor_options(selected_station):
    with data_lock:
        if selected_station == "Any":
            sensors = sorted({sensor for _, sensor in data_store.keys()})
        else:
            sensors = sorted(
                {sensor for station, sensor in data_store.keys() if station == selected_station}
            )
    return [{"label": sensor, "value": sensor} for sensor in sensors]


def apply_stats_event(evt):
    key = (evt["station"], evt["sensor"])
    mean = evt["mean"]
    # Guard against NaN/Inf
    if not isinstance(mean, (int, float)) or not (float("-inf") < float(mean) < float("inf")):
        logger.warning("event=apply_stats_event_invalid_mean", station=key[0], sensor=key[1], mean=mean)
        return
    with data_lock:
        entry = data_store[key]
        entry["timestamps"].append(datetime.fromtimestamp(evt["timestamp"] / 1000))
        entry["actuals"].append(mean)
        if len(entry["timestamps"]) > MAX_POINTS -3:
            entry["timestamps"].pop(0)
            entry["actuals"].pop(0)


def apply_forecast_event(evt):
    key = (evt["station"], evt["sensor"])

    ts_list = [datetime.fromisoformat(ts) for ts in evt.get("timestamps", [])]
    forecasts_list = evt.get("forecasts", [])
    lower_list = evt.get("lower_ci", [])
    upper_list = evt.get("upper_ci", [])

    with data_lock:
        entry = data_store[key]

        if ts_list and forecasts_list and len(ts_list) == len(forecasts_list):
            n = len(ts_list)
            trim_len = max(n - 1, 0)

            if trim_len > 0:
                if len(entry["forecast_ts"]) >= trim_len:
                    entry["forecast_ts"] = entry["forecast_ts"][:-trim_len]
                    entry["forecasts"] = entry["forecasts"][:-trim_len]
                    entry["lower_ci"] = entry.get("lower_ci", [])[:-trim_len]
                    entry["upper_ci"] = entry.get("upper_ci", [])[:-trim_len]
                else:
                    entry["forecast_ts"] = []
                    entry["forecasts"] = []
                    entry["lower_ci"] = []
                    entry["upper_ci"] = []

            entry["forecast_ts"].extend(ts_list)
            entry["forecasts"].extend(forecasts_list)
            entry["lower_ci"].extend(lower_list)
            entry["upper_ci"].extend(upper_list)

        if len(entry["forecast_ts"]) > MAX_POINTS:
            entry["forecast_ts"] = entry["forecast_ts"][-MAX_POINTS:]
            entry["forecasts"] = entry["forecasts"][-MAX_POINTS:]
            entry["lower_ci"] = entry["lower_ci"][-MAX_POINTS:]
            entry["upper_ci"] = entry["upper_ci"][-MAX_POINTS:]

# -------------------------
# Update station dropdown dynamically
# -------------------------
@app.callback(
    Output("station-dropdown", "options"),
    Input("interval", "n_intervals")
)
def update_station_options(_):
    return get_station_options()

# -------------------------
# Update sensor dropdown based on station selection
# -------------------------
@app.callback(
    Output("sensor-dropdown", "options"),
    Input("station-dropdown", "value")
)
def update_sensor_options(selected_station):
    return get_sensor_options(selected_station)

@app.callback(
    Output("sensor-dropdown", "value"),
    Input("station-dropdown", "value")
)
def set_default_sensor(selected_station):
    sensors = [option["value"] for option in get_sensor_options(selected_station)]
    return sensors[0] if sensors else None
# -------------------------
# Dash graph callback
# -------------------------
# -------------------------
# Graph layout inside callback
# -------------------------
@app.callback(
    Output("live-graph", "figure"),
    [Input("interval", "n_intervals"),
     Input("station-dropdown", "value"),
     Input("sensor-dropdown", "value")]
)
def update_graph(_, selected_station, selected_sensor):
    fig = go.Figure()

    with data_lock:
        snapshot = list(data_store.items())

    for (station, sensor), data in snapshot:
        if selected_station != "Any" and station != selected_station:
            continue
        if selected_sensor is not None and sensor != selected_sensor:
            continue

        

        for (station, sensor), data in snapshot:
            if selected_station != "Any" and station != selected_station:
                continue
            if selected_sensor is not None and sensor != selected_sensor:
                continue

            # Actuals: blue
            if data["timestamps"]:
                fig.add_trace(go.Scatter(
                    x=data["timestamps"],
                    y=data["actuals"],
                    mode="lines+markers",
                    name=f"{station} Actual",
                    line=dict(color="#2196f3", width=2),
                    marker=dict(size=6, symbol="circle", opacity=0.8, color="#2196f3")
                ))

            # Forecast with confidence interval: yellow
            if data.get("forecast_ts") and data.get("forecasts") and data.get("lower_ci") and data.get("upper_ci"):
                # Confidence interval shading
                fig.add_trace(go.Scatter(
                    x=data["forecast_ts"],
                    y=data["upper_ci"],
                    mode="lines",
                    line=dict(width=0),
                    fill=None,
                    showlegend=False
                ))
                fig.add_trace(go.Scatter(
                    x=data["forecast_ts"],
                    y=data["lower_ci"],
                    mode="lines",
                    line=dict(width=0),
                    fill='tonexty',
                    fillcolor='rgba(255,255,0,0.10)',
                    showlegend=False
                ))
                # Forecast line
                fig.add_trace(go.Scatter(
                    x=data["forecast_ts"],
                    y=data["forecasts"],
                    mode="lines",
                    line=dict(color="#FFD600", width=3, dash="dash"),
                    name=f"{station} Forecast"
                ))

                fig.update_layout(
                    autosize=True,
                    xaxis=dict(
                        title="Time",
                        showgrid=True,
                        gridcolor="rgba(255,255,255,0.05)",
                        zeroline=False,
                        showline=True,
                        linecolor="#333",
                        tickfont=dict(size=12)
                    ),
                    yaxis=dict(
                        title="Value",
                        showgrid=True,
                        gridcolor="rgba(255,255,255,0.05)",
                        zeroline=False,
                        showline=True,
                        linecolor="#333",
                        tickfont=dict(size=12)
                    ),
                    template="plotly_dark",
                    plot_bgcolor="#23272b",
                    paper_bgcolor="#181c20",
                    font=dict(color="#FFF", family="Arial"),
                    legend=dict(
                        bgcolor="#222",
                        bordercolor="#444",
                        borderwidth=1,
                        orientation="h",
                        yanchor="bottom",
                        y=1.02,
                        xanchor="center",
                        x=0.5,
                        font=dict(size=12)
                    ),
                    hovermode="x unified",
                    margin=dict(l=40, r=40, t=60, b=40)
                )
    return fig



# -------------------------
# NATS consumer
# -------------------------
async def consume_events(stop_event: asyncio.Event):
    backoff_sec = INITIAL_RETRY_BACKOFF_SEC

    while not stop_event.is_set():
        nc = NATS()

        try:
            await nc.connect(servers=[NATS_URL])
            logger.info("nats_connected", nats_url=NATS_URL)
            backoff_sec = INITIAL_RETRY_BACKOFF_SEC

            async def handle_stats(msg):
                try:
                    evt = json.loads(msg.data.decode())
                    apply_stats_event(evt)
                except Exception as exc:
                    logger.error("handle_stats_failed", error=str(exc))

            async def handle_forecast(msg):
                try:
                    evt = json.loads(msg.data.decode())
                    apply_forecast_event(evt)
                except Exception as exc:
                    logger.error("handle_forecast_failed", error=str(exc))

            await nc.subscribe("stats", cb=handle_stats)
            await nc.subscribe("forecasts", cb=handle_forecast)
            logger.info("subscribed", topics=["stats", "forecasts"])

            await stop_event.wait()
        except Exception as exc:
            logger.error("nats_connect_failed", backoff_sec=backoff_sec, error=str(exc))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff_sec)
            except asyncio.TimeoutError:
                pass
            backoff_sec = min(backoff_sec * 2, MAX_RETRY_BACKOFF_SEC)
        finally:
            if nc.is_connected:
                await nc.drain()
                logger.info("nats_drained")

# -------------------------
# Run both Dash and NATS consumer
# -------------------------
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # Run Dash in a separate thread
    def run_dash():
        app.run(host="0.0.0.0", port=8004, debug=False)
    Thread(target=run_dash, daemon=True).start()

    logger.info("started", url="http://localhost:8004")
    try:
        loop.run_until_complete(consume_events(stop_event))
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    finally:
        stop_event.set()
        loop.close()
