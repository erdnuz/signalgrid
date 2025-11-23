import asyncio
import json
import os
from collections import defaultdict
from threading import Thread
from datetime import datetime, timedelta

from dash import Dash, dcc, html
from dash.dependencies import Input, Output
from nats.aio.client import Client as NATS
import plotly.graph_objects as go

# -------------------------
# Configuration
# -------------------------
NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")
MAX_POINTS = 50  # points to store per line

app = Dash(__name__)
app.layout = html.Div([
    # Title
    html.H1("Live Sensor Forecast vs Actual", style={
        "textAlign": "center",
        "color": "#FFA500",
        "marginBottom": "20px",
        "fontFamily": "Arial, sans-serif"
    }),

    # Dropdowns in a vertical column
    html.Div([
        html.Div([
        html.Label("Select Station:", style={"fontWeight": "bold", "color": "#FFF", "padding":"5px", "display": "block"}),
        dcc.Dropdown(
            id="station-dropdown",
            options=[{"label": "Any", "value": "Any"}],
            value="Any",
            clearable=False,
            style={"backgroundColor": "#222", "color": "#FFF", "borderRadius": "6px", "marginBottom": "10px"}
        )]),
        html.Div([
        html.Label("Select Sensor:", style={"fontWeight": "bold", "color": "#FFF", "padding":"5px", "display": "block"}),
        dcc.Dropdown(
            id="sensor-dropdown",
            options=[],
            value=None,
            clearable=False,
            style={"backgroundColor": "#222", "color": "#FFF", "borderRadius": "6px"}
        )]),
    ], style={
        "width": "300px",
        "margin": "0 auto 20px auto",
        "padding": "15px",
        "backgroundColor": "#1E1E1E",
        "borderRadius": "12px",
        "boxShadow": "0 4px 12px rgba(0,0,0,0.5)",
        "display": "flex",
        "flexDirection": "row",
        "justifyContent": "space-between",  # distribute space between dropdowns
    "gap": "10px"  
    }),

    # Graph
    dcc.Graph(id="live-graph", style={
        "height": "700px",
        "width": "95%",
        }, config={
        "displayModeBar": False,  # disables the toolbar entirely
        "scrollZoom": False       # also disables zoom by scroll
    }),

    # Update interval
    dcc.Interval(id="interval", interval=1000),  # 1s
], style={
    "backgroundColor": "#121212",
    "margin": "0",
    "padding": "20px",
    "color": "#FFF",
    "fontFamily": "Arial, sans-serif",
    "minHeight": "100vh",
    "display": "flex",
    "flexDirection": "column",
    "alignItems": "center"
})


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

@app.callback(
    Output("sensor-dropdown", "value"),
    Input("station-dropdown", "value")
)
def set_default_sensor(selected_station):
    # Get sensors for selected station
    if selected_station == "Any":
        sensors = sorted({sensor for _, sensor in data_store.keys()})
    else:
        sensors = sorted({sensor for station, sensor in data_store.keys() if station == selected_station})

    # Return the first sensor as default, or None if no sensors
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

    for (station, sensor), data in data_store.items():
        if selected_station != "Any" and station != selected_station:
            continue
        if selected_sensor is not None and sensor != selected_sensor:
            continue

        # Actuals
        if data["timestamps"]:
            fig.add_trace(go.Scatter(
                x=data["timestamps"],
                y=data["actuals"],
                mode="lines+markers",
                name=f"{station}:{sensor} Actual",
                line=dict(color="#00BCD4", width=2),
                marker=dict(size=6, symbol="circle", opacity=0.8)
            ))

        # Forecast with confidence interval
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
                fillcolor='rgba(255,165,0,0.2)',
                showlegend=True,
                name=f"{station}:{sensor} CI"
            ))
            # Forecast line
            fig.add_trace(go.Scatter(
                x=data["forecast_ts"],
                y=data["forecasts"],
                mode="lines",
                line=dict(color="#FFA500", width=3, dash="dash"),
                name=f"{station}:{sensor} Forecast"
            ))

    fig.update_layout(
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
        plot_bgcolor="#1E1E1E",
        paper_bgcolor="#121212",
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
async def consume_events():
    nc = NATS()
    await nc.connect(servers=[NATS_URL])

    async def handle_stats(msg):
        evt = json.loads(msg.data.decode())
        key = (evt["station"], evt["sensor"])
        entry = data_store[key]

        entry["timestamps"].append(datetime.fromtimestamp(evt["timestamp"] / 1000))
        entry["actuals"].append(evt["mean"])
        if len(entry["timestamps"]) > MAX_POINTS:
            entry["timestamps"].pop(0)
            entry["actuals"].pop(0)

    async def handle_forecast(msg):
        evt = json.loads(msg.data.decode())
        key = (evt["station"], evt["sensor"])
        entry = data_store[key]

        ts_list = [datetime.fromisoformat(ts) for ts in evt.get("timestamps", [])]
        forecasts_list = evt.get("forecasts", [])
        lower_list = evt.get("lower_ci", [])
        upper_list = evt.get("upper_ci", [])

        if ts_list and forecasts_list and len(ts_list) == len(forecasts_list):
            n = len(ts_list)
            # Remove last n-1 old points
            if len(entry["forecast_ts"]) >= n - 1:
                entry["forecast_ts"] = entry["forecast_ts"][:-(n-1)]
                entry["forecasts"] = entry["forecasts"][:-(n-1)]
                entry["lower_ci"] = entry.get("lower_ci", [])[:-(n-1)]
                entry["upper_ci"] = entry.get("upper_ci", [])[:-(n-1)]

            # Append new points
            entry["forecast_ts"].extend(ts_list)
            entry["forecasts"].extend(forecasts_list)
            entry["lower_ci"].extend(lower_list)
            entry["upper_ci"].extend(upper_list)

        # Trim to MAX_POINTS
        if len(entry["forecast_ts"]) > MAX_POINTS:
            entry["forecast_ts"] = entry["forecast_ts"][-MAX_POINTS:]
            entry["forecasts"] = entry["forecasts"][-MAX_POINTS:]
            entry["lower_ci"] = entry["lower_ci"][-MAX_POINTS:]
            entry["upper_ci"] = entry["upper_ci"][-MAX_POINTS:]

    await nc.subscribe("stats", cb=handle_stats)
    await nc.subscribe("forecasts", cb=handle_forecast)

# -------------------------
# Run both Dash and NATS consumer
# -------------------------
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(consume_events())

    # Run Dash in a separate thread
    def run_dash():
        app.run(host="0.0.0.0", port=8004, debug=False)
    Thread(target=run_dash, daemon=True).start()

    print("Forecast live plot running at http://localhost:8004")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("Exiting...")
