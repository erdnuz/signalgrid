"""Dash application factory."""

from __future__ import annotations

from typing import Any

from dash import Dash, Input, Output, State, dcc, html

from .config import Settings
from .figures import build_figure
from .store import DataStore, Kpis

KPI_TILES = [
    ("kpi-mae", "Forecast MAE", "1-step, rolling"),
    ("kpi-coverage", "Interval coverage", "nominal —"),
    ("kpi-regime", "Regime accuracy", "detected vs. true"),
    ("kpi-latency", "Pipeline latency", "window close → UI, p50"),
    ("kpi-throughput", "Throughput", "windows / s"),
]


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "—" if value is None else f"{value:{spec}}{suffix}"


def kpi_values(k: Kpis) -> list[str]:
    return [
        _fmt(k.mae, ".3f"),
        _fmt(k.coverage, ".1%"),
        _fmt(k.regime_accuracy, ".1%"),
        _fmt(k.latency_ms_p50, ".0f", " ms"),
        _fmt(k.windows_per_s, ".1f"),
    ]


def choose(values: list[Any], current: Any) -> Any:
    """Keep the user's selection while it is still valid, else pick the first option."""
    if current in values:
        return current
    return values[0] if values else None


def _tile(tile_id: str, label: str, note: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, className="kpi-label"),
            html.Div("—", id=tile_id, className="kpi-value"),
            html.Div(note, id=f"{tile_id}-note", className="kpi-note"),
        ],
        className="kpi",
    )


def build_layout(settings: Settings) -> html.Div:
    return html.Div(
        [
            html.Header(
                [
                    html.H1("SignalGrid", className="title"),
                    html.P(
                        "Live window statistics, AR(1) forecasts with prediction intervals, "
                        "and online Markov-switching regime detection.",
                        className="subtitle",
                    ),
                ]
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Station", htmlFor="station-dropdown", className="label"),
                            dcc.Dropdown(id="station-dropdown", clearable=False, searchable=False),
                        ],
                        className="control",
                    ),
                    html.Div(
                        [
                            html.Label("Sensor", htmlFor="sensor-dropdown", className="label"),
                            dcc.Dropdown(id="sensor-dropdown", clearable=False, searchable=False),
                        ],
                        className="control",
                    ),
                ],
                className="controls",
            ),
            html.Div([_tile(*t) for t in KPI_TILES], className="kpis"),
            dcc.Graph(
                id="live-graph",
                className="chart",
                config={"displayModeBar": False, "scrollZoom": False},
            ),
            dcc.Interval(id="interval", interval=settings.refresh_ms),
        ],
        className="container",
    )


def create_app(settings: Settings, store: DataStore, health: Any = None) -> Dash:
    app = Dash(__name__, title="SignalGrid", update_title="")
    app.layout = build_layout(settings)

    @app.server.get("/health")  # type: ignore[untyped-decorator]
    def _health() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"status": "ok", "service": "frontend", **store.counts()}
        if health is not None:
            body["nats_connected"] = bool(health())
        return body, 200

    @app.callback(
        Output("station-dropdown", "options"),
        Output("station-dropdown", "value"),
        Input("interval", "n_intervals"),
        State("station-dropdown", "value"),
    )
    def _stations(_: int, current: str | None) -> tuple[list[str], str | None]:
        stations = store.stations()
        return stations, choose(stations, current)

    @app.callback(
        Output("sensor-dropdown", "options"),
        Output("sensor-dropdown", "value"),
        Input("station-dropdown", "value"),
        Input("interval", "n_intervals"),
        State("sensor-dropdown", "value"),
    )
    def _sensors(station: str | None, _: int, current: int | None) -> tuple[list[dict[str, Any]], int | None]:
        sensors = store.sensors(station)
        options = [{"label": f"Sensor {s}", "value": s} for s in sensors]
        return options, choose(sensors, current)

    @app.callback(
        Output("live-graph", "figure"),
        *[Output(tile_id, "children") for tile_id, _, _ in KPI_TILES],
        Output("kpi-coverage-note", "children"),
        Input("interval", "n_intervals"),
        Input("station-dropdown", "value"),
        Input("sensor-dropdown", "value"),
    )
    def _refresh(_: int, station: str | None, sensor: int | None) -> tuple[Any, ...]:
        series = store.series(station, sensor) if station is not None and sensor is not None else None
        regimes = store.regimes(station) if station is not None else None
        kpis = store.kpis(station, sensor)
        nominal = f"nominal {kpis.confidence:.0%}" if kpis.confidence else "nominal —"
        return (build_figure(series, regimes), *kpi_values(kpis), nominal)

    return app
