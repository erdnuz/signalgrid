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
MAX_POINTS = 100  # points to store per line

# -------------------------
# Storage for live plotting
# key: (station, sensor) -> list of (timestamp, mean)
data_store = defaultdict(lambda: {"timestamps": [], "means": []})

# -------------------------
# Dash app setup
# -------------------------
app = Dash(__name__)
app.layout = html.Div([
    html.H1("Live Sensor Forecast Data"),
    dcc.Interval(id="interval", interval=2000),  # update every 2s
    dcc.Graph(id="live-graph")
])

# -------------------------
# Dash callback
# -------------------------
@app.callback(
    Output("live-graph", "figure"),
    Input("interval", "n_intervals")
)
def update_graph(_):

    fig = go.Figure()
    for (station, sensor), data in data_store.items():
        if data["timestamps"]:
            fig.add_trace(go.Scatter(
                x=data["timestamps"],
                y=data["means"],
                mode="lines+markers",
                name=f"{station}:{sensor}"
            ))
    fig.update_layout(
        xaxis_title="Timestamp",
        yaxis_title="Mean Value",
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

            key = (station, sensor)
            entry = data_store[key]
            entry["timestamps"].append(ts)
            entry["means"].append(mean_val)

            if len(entry["timestamps"]) > MAX_POINTS:
                entry["timestamps"].pop(0)
                entry["means"].pop(0)
        except Exception as e:
            print("Failed to process message:", e)

    await nc.subscribe("stats", cb=handler)

# -------------------------
# Run both Dash and NATS consumer
# -------------------------
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(consume_stats())
    # Run Dash in the same event loop
    from threading import Thread
    def run_dash():
        app.run(host="0.0.0.0", port=8004, debug=False)
    Thread(target=run_dash, daemon=True).start()
    print("Forecast live plot running at http://localhost:8004")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("Exiting...")
