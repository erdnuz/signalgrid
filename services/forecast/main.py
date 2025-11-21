import asyncio
import json
import os
from collections import defaultdict

from dash import Dash, dcc, html
from dash.dependencies import Input, Output
from nats.aio.client import Client as NATS
import plotly.graph_objects as go

# -------------------------
# Configuration
# -------------------------
NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")
MAX_POINTS = 500  # points to store per line


# -------------------------
# Dash app setup
# -------------------------
app = Dash(__name__)
app.layout = html.Div([
    html.H1("Live Sensor Forecast Data"),
    
    html.Div([
        html.Label("Select Station:"),
        dcc.Dropdown(
            id="station-dropdown",
            options=[{"label": "Any", "value": "Any"}],
            value="Any",
            clearable=False
        ),
        html.Label("Select Sensor:"),
        dcc.Dropdown(
            id="sensor-dropdown",
            options=[],
            value=None,
            clearable=False
        ),
    ], style={"width": "30%", "display": "inline-block", "verticalAlign": "top"}),

    dcc.Interval(id="interval", interval=2000),  # update every 2s
    dcc.Graph(id="live-graph")
])

# -------------------------
# Update station dropdown dynamically
# -------------------------
@app.callback(
    Output("station-dropdown", "options"),
    Input("interval", "n_intervals")
)
def update_station_options(_):
    stations = sorted({station for station, _ in data_store.keys()})
    options = [{"label": "Any", "value": "Any"}] + [{"label": s, "value": s} for s in stations]
    return options

# -------------------------
# Update sensor dropdown based on station selection
# -------------------------
@app.callback(
    Output("sensor-dropdown", "options"),
    Input("station-dropdown", "value")
)
def update_sensor_options(selected_station):
    if selected_station == "Any":
        sensors = sorted({sensor for _, sensor in data_store.keys()})
    else:
        sensors = sorted({sensor for station, sensor in data_store.keys() if station == selected_station})
    return [{"label": s, "value": s} for s in sensors]

# -------------------------
# Storage for live plotting
# key: (station, sensor) -> dict of lists
data_store = defaultdict(lambda: {
    "timestamps": [], 
    "means": [], 
    "maxs": [], 
    "mins": []
})

# -------------------------
# Dash graph callback
# -------------------------
@app.callback(
    Output("live-graph", "figure"),
    [Input("interval", "n_intervals"),
     Input("station-dropdown", "value"),
     Input("sensor-dropdown", "value")]
)
def update_graph(_, selected_station, selected_sensor):
    fig = go.Figure()
    for (station, sensor), data in data_store.items():
        if data["timestamps"]:
            if selected_station == "Any" or station == selected_station:
                if selected_sensor is None or sensor == selected_sensor:
                    # Plot mean
                    fig.add_trace(go.Scatter(
                        x=data["timestamps"],
                        y=data["means"],
                        mode="lines+markers",
                        name=f"{station}:{sensor} (mean)"
                    ))
                    # Plot max
                    fig.add_trace(go.Scatter(
                        x=data["timestamps"],
                        y=data["maxs"],
                        mode="lines",
                        line=dict(dash="dash"),
                        name=f"{station}:{sensor} (max)"
                    ))
                    # Plot min
                    fig.add_trace(go.Scatter(
                        x=data["timestamps"],
                        y=data["mins"],
                        mode="lines",
                        line=dict(dash="dot"),
                        name=f"{station}:{sensor} (min)"
                    ))
    fig.update_layout(
        xaxis_title="Timestamp",
        yaxis_title="Value",
        template="plotly_dark"
    )
    return fig

# -------------------------
# NATS consumer
# -------------------------
async def consume_stats():
    nc = NATS()
    await nc.connect(servers=[NATS_URL])

    async def handler(msg):
        try:
            stat = json.loads(msg.data.decode())
            station = stat["station"]
            sensor = stat["sensor"]
            ts = stat["timestamp"]
            mean_val = stat["mean"]
            max_val = stat["max"]
            min_val = stat["min"]

            key = (station, sensor)
            entry = data_store[key]

            entry["timestamps"].append(ts)
            entry["means"].append(mean_val)
            entry["maxs"].append(max_val)
            entry["mins"].append(min_val)

            if len(entry["timestamps"]) > MAX_POINTS:
                entry["timestamps"].pop(0)
                entry["means"].pop(0)
                entry["maxs"].pop(0)
                entry["mins"].pop(0)
        except Exception as e:
            print("Failed to process message:", e)

    await nc.subscribe("stats", cb=handler)


# -------------------------
# Run both Dash and NATS consumer
# -------------------------
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(consume_stats())

    # Run Dash in a separate thread
    from threading import Thread
    def run_dash():
        app.run(host="0.0.0.0", port=8004, debug=False)
    Thread(target=run_dash, daemon=True).start()

    print("Forecast live plot running at http://localhost:8004")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("Exiting...")
