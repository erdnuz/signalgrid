"""Dash application factory.

Views
-----
* **Single**: one station and sensor in detail (forecast band, horizon,
  regime strip).
* **Compare**: any stations x sensors overlaid, optionally z-scored.
* **Aggregate**: per station, the mean across the selected sensors with the
  min-max envelope.

The station x sensor health grid (click a cell to inspect it), the
correlation matrix (click a cell to compare that pair) and the alert feed
(click an alert to inspect its series) tie the views together.

Update model
------------
A fast interval (`REFRESH_MS`, 250 ms) runs one callback that, per time
chart, either re-renders (view, selection, range, scale or data epoch
changed), sends only the new points through `extendData`, or sends nothing.
What the browser holds is tracked in a client-side cursor (`dcc.Store`), so
the server keeps no per-client state. Matrix views and the alert feed change
slowly and refresh every 2 s, and only when their content changed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from . import charts
from .charts import RANGES_S, Chart, Trace
from .config import Settings
from .models import AlertMessage
from .store import HORIZONS, DataStore, Key, Kpis, SeriesView

VIEWS = [("single", "Single"), ("compare", "Compare"), ("aggregate", "Aggregate")]
RANGES = [(k, k) for k in RANGES_S]
SCALES = [("raw", "Raw"), ("z", "z-score")]
H_SHORT, H_LONG = HORIZONS

KPI_TILES = [
    ("kpi-mase", "Forecast skill (MASE)", f"{H_SHORT}-step · {H_LONG}-step vs. persistence, < 1 is better"),
    ("kpi-coverage", "Interval coverage", "—"),
    ("kpi-regime", "Regime accuracy", "detected vs. true"),
    ("kpi-alerts", "Active alerts", "—"),
    ("kpi-latency", "Pipeline latency", "window close → dashboard, p50"),
    ("kpi-throughput", "Throughput", "windows / s, all series"),
]

PANELS = [
    (
        f"mase{H_SHORT}-graph",
        f"Forecast skill · {H_SHORT} step ahead",
        "Rolling MASE vs. persistence at the same horizon (dashed: persistence = 1)",
    ),
    (
        f"mase{H_LONG}-graph",
        f"Forecast skill · {H_LONG} steps ahead",
        "Rolling MASE vs. persistence at the same horizon (dashed: persistence = 1)",
    ),
    (
        f"cov{H_SHORT}-graph",
        f"Interval coverage · {H_SHORT} step ahead",
        "Share of actuals inside the forecast interval (dashed: nominal)",
    ),
    (
        f"cov{H_LONG}-graph",
        f"Interval coverage · {H_LONG} steps ahead",
        "Share of actuals inside the forecast interval (dashed: nominal)",
    ),
    (
        "accuracy-graph",
        "Regime detection accuracy",
        "Online Markov-switching filter vs. simulator ground truth",
    ),
    ("latency-graph", "Pipeline latency", "Window close → dashboard receipt, per-second median"),
]
TIME_GRAPHS = ["main-graph", *[p[0] for p in PANELS]]

SEVERITY = {
    "info": ("ℹ", "Info"),
    "warning": ("▲", "Warning"),
    "serious": ("◆", "Serious"),
    "critical": ("■", "Critical"),
}
RULE_LABELS = {
    "anomaly": "Anomaly",
    "calibration": "Calibration drift",
    "model_degraded": "Model degraded",
    "regime_change": "Regime change",
    "stale": "Stale data",
    "regime_accuracy": "Regime detection degraded",
    "latency": "High latency",
}


# ---- small helpers ---------------------------------------------------------


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "—" if value is None else f"{value:{spec}}{suffix}"


def kpi_values(k: Kpis) -> list[str]:
    return [
        f"{_fmt(k.mase.get(H_SHORT), '.2f')} · {_fmt(k.mase.get(H_LONG), '.2f')}",
        f"{_fmt(k.coverage.get(H_SHORT), '.1%')} · {_fmt(k.coverage.get(H_LONG), '.1%')}",
        _fmt(k.regime_accuracy, ".1%"),
        str(k.active_alerts),
        _fmt(k.latency_ms_p50, ".0f", " ms"),
        _fmt(k.windows_per_s, ".1f"),
    ]


def choose(values: list[Any], current: Any) -> Any:
    """Keep the user's selection while it is still valid, else pick the first option."""
    if current in values:
        return current
    return values[0] if values else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def coerce_selection(view: str, value: Any, options: Sequence[Any], *, default_all: bool) -> Any:
    """Single views hold one value, multi views a list; invalid entries drop out."""
    valid = [v for v in _as_list(value) if v in options]
    if view == "single":
        return choose(list(options), valid[0] if valid else None)
    if default_all and len(valid) <= 1:
        return list(options)
    return valid or list(options[:1])


def resolve(view: str, stations: Any, sensors: Any, keys: Sequence[Key]) -> tuple[list[Key], list[str]]:
    """Selected (station, sensor) keys and stations for a view."""
    sts, sns = _as_list(stations), _as_list(sensors)
    if view == "single":
        sts, sns = sts[:1], sns[:1]
    selected = [k for k in keys if k[0] in sts and k[1] in sns]
    return selected, [s for s in sts if any(k[0] == s for k in keys)]


def _ts(ms: int, offset_min: int = 0) -> str:
    """Wall-clock time in the viewer's zone (charts use the browser's zone too)."""
    return datetime.fromtimestamp(ms / 1000 + offset_min * 60, tz=UTC).strftime("%H:%M:%S")


def _aria(**attrs: str) -> dict[str, Any]:
    """`aria-*` attributes for Dash html components."""
    return {f"aria-{k}": v for k, v in attrs.items()}


def _hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---- layout ----------------------------------------------------------------


def segmented(group: str, label: str, options: Sequence[tuple[str, str]], value: str) -> html.Div:
    """Button group with one pressed option (styled as a segmented control)."""
    return html.Div(
        [
            html.Span(label, className="label", id=f"{group}-label"),
            html.Div(
                [
                    html.Button(
                        text,
                        id={"type": f"seg-{group}", "value": v},
                        n_clicks=0,
                        className="seg-btn selected" if v == value else "seg-btn",
                        **_aria(pressed="true" if v == value else "false"),
                    )
                    for v, text in options
                ],
                className="segmented",
                role="group",
                **_aria(labelledby=f"{group}-label"),
            ),
        ],
        className="control control-seg",
        id=f"{group}-control",
    )


def _tile(tile_id: str, label: str, note: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, className="kpi-label"),
            html.Div("—", id=tile_id, className="kpi-value"),
            html.Div(note, id=f"{tile_id}-note", className="kpi-note"),
        ],
        className="kpi",
    )


def _panel(graph_id: str, title: str, note: str) -> html.Section:
    return html.Section(
        [
            html.H2(title, className="panel-title"),
            html.P(note, className="panel-note"),
            dcc.Graph(id=graph_id, config={"displayModeBar": False}, className="panel-graph"),
        ],
        className="panel",
    )


def build_layout(settings: Settings) -> html.Div:
    dropdown: dict[str, Any] = dict(clearable=False, searchable=False)
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
                    segmented("view", "View", VIEWS, "single"),
                    html.Div(
                        [
                            html.Label("Stations", htmlFor="station-dropdown", className="label"),
                            dcc.Dropdown(id="station-dropdown", **dropdown),
                        ],
                        className="control control-select",
                    ),
                    html.Div(
                        [
                            html.Label("Sensors", htmlFor="sensor-dropdown", className="label"),
                            dcc.Dropdown(id="sensor-dropdown", **dropdown),
                        ],
                        className="control control-select",
                    ),
                    segmented("range", "Time range", RANGES, charts.DEFAULT_RANGE),
                    segmented("scale", "Scale", SCALES, "raw"),
                ],
                className="controls",
            ),
            html.Div([_tile(*t) for t in KPI_TILES], className="kpis"),
            html.Div(
                [
                    html.Section(
                        [
                            html.H2(id="main-title", className="panel-title"),
                            html.P(id="main-note", className="panel-note"),
                            dcc.Graph(id="main-graph", config={"displayModeBar": False, "scrollZoom": False}),
                        ],
                        className="chart",
                    ),
                    html.Section(
                        [
                            html.H2("Alerts", className="panel-title"),
                            html.P(id="alerts-note", className="panel-note"),
                            html.Div(id="alert-feed", className="alert-feed"),
                        ],
                        className="panel alerts",
                    ),
                ],
                className="main-row",
            ),
            html.Div([_panel(*p) for p in PANELS], className="panels"),
            html.Div(
                [
                    _panel(
                        "health-graph",
                        "Sensor health",
                        f"{H_SHORT}-step MASE and interval coverage per sensor · click a cell to inspect it",
                    ),
                    _panel(
                        "corr-graph",
                        "Correlation of window-to-window changes",
                        "Over the selected range · click a cell to compare the pair",
                    ),
                ],
                className="panels",
            ),
            dcc.Store(id="view-mode", data="single"),
            dcc.Store(id="time-range", data=charts.DEFAULT_RANGE),
            dcc.Store(id="scale", data="raw"),
            dcc.Store(id="chart-cursor"),
            dcc.Store(id="slow-hash"),
            dcc.Store(id="tz-offset", data=0),
            dcc.Interval(id="interval", interval=settings.refresh_ms),
            dcc.Interval(id="discovery", interval=2_000),
        ],
        className="container",
    )


# ---- chart specs -----------------------------------------------------------


def _aligned_mean(views: Sequence[SeriesView], attr: str, horizon: int) -> tuple[list[int], list[float]]:
    """Mean of a per-series metric history across series, aligned on window."""
    maps = [
        {t: v for t, v in zip(s.metric_ts, getattr(s, attr)[horizon], strict=True) if v is not None}
        for s in views
    ]
    if not maps:
        return [], []
    common = sorted(set(maps[0]).intersection(*maps[1:]))
    return common, [sum(m[t] for m in maps) / len(maps) for t in common]


def build_charts(
    store: DataStore,
    view: str,
    selected: dict[Key, SeriesView],
    stations: Sequence[str],
    scale: str,
    end_ms: int,
    range_ms: int,
) -> tuple[dict[str, Chart], dict[str, str]]:
    all_keys = store.keys()
    all_stations = store.stations()
    views = list(selected.values())
    regimes = {st: r for st in stations if (r := store.regimes(st)) is not None}
    nominal = next((v.confidence for v in views if v.confidence), None) or 0.98

    if view == "single":
        v = views[0] if views else None
        main = charts.detail_chart(v, regimes.get(v.station) if v else None)
        titles = {
            "title": f"{v.station} · Sensor {v.sensor}" if v else "Detail",
            "note": "Window means, 1-step-ahead forecasts with the prediction band, and the regime detector",
        }
    elif view == "compare":
        main = charts.compare_chart(views, all_keys, scale == "z", end_ms - range_ms)
        titles = {
            "title": f"Comparing {len(views)} series",
            "note": "z-scored over the visible range: compare shape, not level"
            if scale == "z"
            else "Raw window means",
        }
    else:
        groups: dict[str, list[SeriesView]] = {}
        for v in views:
            groups.setdefault(v.station, []).append(v)
        main = charts.aggregate_chart(groups, all_stations)
        titles = {
            "title": "Station averages",
            "note": "Mean across the selected sensors, shaded from the lowest to the highest sensor",
        }

    def metric_lines(attr: str, horizon: int) -> list[tuple[str, str, Trace]]:
        if view == "single":
            return [
                (
                    "forecast",
                    charts.FORECAST,
                    Trace(v.metric_ts, getattr(v, attr)[horizon], interval_ms=v.window_ms),
                )
                for v in views
            ]
        if view == "compare":
            return [
                (
                    charts.series_label((v.station, v.sensor)),
                    charts.series_color((v.station, v.sensor), all_keys),
                    Trace(v.metric_ts, getattr(v, attr)[horizon], interval_ms=v.window_ms),
                )
                for v in views
            ]
        out = []
        for st in stations:
            members = [v for v in views if v.station == st]
            ts, mean = _aligned_mean(members, attr, horizon)
            if ts:
                trace = Trace(ts, mean, interval_ms=members[0].window_ms)
                out.append((f"{st} mean", charts.station_color(st, all_stations), trace))
        return out

    def skill(horizon: int) -> Chart:
        return charts.metric_chart(
            metric_lines("mase", horizon),
            y_title="MASE",
            y_format=".2f",
            empty="Waiting for scored forecasts…",
            include=[1.0],
            reference=(1.0, "persistence"),
        )

    def coverage(horizon: int) -> Chart:
        return charts.metric_chart(
            metric_lines("coverage", horizon),
            y_title="Coverage",
            y_format=".1%",
            empty="Waiting for scored forecasts…",
            include=[nominal, 1.0],
            reference=(nominal, f"nominal {nominal:.0%}"),
        )

    accuracy_lines = [
        (
            st,
            charts.DETECTED if len(regimes) == 1 else charts.station_color(st, all_stations),
            Trace(r.timestamps, r.accuracy, interval_ms=500),
        )
        for st, r in regimes.items()
    ]
    lat_ts, lat = store.latency_history()

    specs = {
        "main-graph": main,
        f"mase{H_SHORT}-graph": skill(H_SHORT),
        f"mase{H_LONG}-graph": skill(H_LONG),
        f"cov{H_SHORT}-graph": coverage(H_SHORT),
        f"cov{H_LONG}-graph": coverage(H_LONG),
        "accuracy-graph": charts.metric_chart(
            accuracy_lines,
            y_title="Accuracy",
            y_format=".0%",
            empty="Waiting for the first regime model fit (~2 min)…",
            y_range=(0.0, 1.0),
        ),
        "latency-graph": charts.metric_chart(
            [("latency", charts.NEUTRAL, Trace(lat_ts, lat, interval_ms=1_000))] if lat_ts else [],
            y_title="ms",
            y_format=".0f",
            empty="Waiting for data…",
        ),
    }
    return specs, titles


def refresh(
    store: DataStore,
    view: str | None,
    stations: Any,
    sensors: Any,
    range_key: str | None,
    scale: str | None,
    cursor: dict[str, Any] | None,
    now_ms: int | None = None,
) -> tuple[list[Any], list[Any], Any, list[Any], list[Any]]:
    """Returns (figures, extendData values, new cursor, KPI outputs, title
    outputs), with `no_update` wherever the client is already current."""
    view = view if view in dict(VIEWS) else "single"
    range_key = range_key if range_key in RANGES_S else charts.DEFAULT_RANGE
    scale = scale if scale in dict(SCALES) else "raw"
    range_ms = RANGES_S[range_key] * 1000
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    end_ms = now - now % 500  # the range anchor moves in whole windows

    keys, sts = resolve(view, stations, sensors, store.keys())
    views = {k: v for k in keys if (v := store.series(*k)) is not None}
    specs, titles = build_charts(store, view, views, sts, scale, end_ms, range_ms)
    epochs = [v.epoch for v in views.values()]
    selection = f"{view}|{sts}|{keys}|{range_key}|{scale}|{epochs}"

    previous = cursor or {}
    new_cursor: dict[str, Any] = {}
    figures: list[Any] = []
    extends: list[Any] = []
    for graph_id in TIME_GRAPHS:
        chart = specs[graph_id]
        traces = charts.traces_of(chart, end_ms, range_ms)
        sig = f"{selection}/{len(traces)}"
        prev = previous.get(graph_id)
        if prev is None or prev.get("sig") != sig:
            figures.append(charts.render_chart(chart, end_ms, range_ms, selection))
            extends.append(no_update)
            new_cursor[graph_id] = {
                "sig": sig,
                "last": charts.cursor_for(traces),
                "affine": charts.affines_of(chart),
            }
        else:
            payload, last = charts.delta(traces, prev["last"], range_ms, prev.get("affine"))
            figures.append(no_update)
            extends.append(payload if payload is not None else no_update)
            new_cursor[graph_id] = {**prev, "last": last}

    kpis = store.kpis(keys, sts)
    nominal = f"{H_SHORT}-step · {H_LONG}-step, nominal {kpis.confidence:.0%}" if kpis.confidence else "—"
    texts = [
        *kpi_values(kpis),
        nominal,
        f"{kpis.severe_alerts} serious or critical" if kpis.active_alerts else "all clear",
    ]
    new_cursor["kpis"] = texts
    kpi_out: list[Any] = [no_update] * len(texts) if previous.get("kpis") == texts else texts
    title_vals = [titles["title"], titles["note"]]
    new_cursor["titles"] = title_vals
    title_out: list[Any] = [no_update] * 2 if previous.get("titles") == title_vals else title_vals

    return figures, extends, (no_update if new_cursor == previous else new_cursor), kpi_out, title_out


def _alert_row(alert: AlertMessage, index: int, offset_min: int = 0) -> html.Button:
    resolved = alert.state == "resolved"
    icon, label = ("✓", "Resolved") if resolved else SEVERITY[alert.severity]
    scope = alert.station if alert.sensor is None else f"{alert.station} · S{alert.sensor}"
    return html.Button(
        [
            html.Span(icon, className="alert-icon", **_aria(hidden="true")),
            html.Span(
                [
                    html.Span(
                        [
                            html.Span(label, className="alert-severity"),
                            f" {RULE_LABELS.get(alert.rule, alert.rule)}",
                        ],
                        className="alert-title",
                    ),
                    html.Span(scope, className="alert-scope"),
                    html.Span(alert.message, className="alert-message"),
                ],
                className="alert-body",
            ),
            html.Span(_ts(alert.timestamp, offset_min), className="alert-time"),
        ],
        id={
            "type": "alert-row",
            "index": index,
            "station": alert.station,
            "sensor": -1 if alert.sensor is None else alert.sensor,
        },
        n_clicks=0,
        className=f"alert-row sev-{'resolved' if resolved else alert.severity}",
        title="Inspect this series",
    )


def slow_refresh(
    store: DataStore,
    view: str | None,
    stations: Any,
    sensors: Any,
    range_key: str | None,
    previous_hash: str | None,
    now_ms: int | None = None,
    offset_min: int = 0,
) -> tuple[Any, Any, Any, Any, Any]:
    """Health grid, correlation matrix, alert feed: rebuilt only on change."""
    view = view if view in dict(VIEWS) else "single"
    range_ms = RANGES_S.get(range_key or "", 60) * 1000
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    keys = store.keys()
    _, sts = resolve(view, stations, sensors, keys)
    all_stations = store.stations()
    all_sensors = sorted({k[1] for k in keys})
    views = [v for k in keys if (v := store.series(*k)) is not None]

    log, active = store.alerts(now)
    scope = set(sts) if view == "single" and sts else set(sts) or set(all_stations)
    feed = [a for a in log if a.station in scope][:25]
    active_by_key: dict[Key, int] = {}
    for a in active:
        if a.sensor is not None:
            active_by_key[(a.station, a.sensor)] = active_by_key.get((a.station, a.sensor), 0) + 1

    m1 = {(v.station, v.sensor): v.metrics.get(H_SHORT) for v in views}
    mase = {k: m.mase if m else None for k, m in m1.items()}
    cov = {k: m.coverage if m else None for k, m in m1.items()}
    corr_keys, matrix = charts.correlation(views, now - range_ms)
    n_active = len([a for a in active if a.station in scope])
    state = {
        "health": [
            all_stations,
            all_sensors,
            {
                str(k): [
                    None if mase[k] is None else round(mase[k] or 0, 2),
                    cov[k] and round(cov[k] or 0, 3),
                ]
                for k in mase
            },
            {str(k): n for k, n in active_by_key.items()},
        ],
        "corr": [corr_keys, matrix],
        "feed": [a.id + a.state for a in feed],
        "scope": sorted(scope),
        "active": n_active,
        "tz": offset_min,
    }
    digest = _hash(state)
    if digest == previous_hash:
        return no_update, no_update, no_update, no_update, no_update

    health = charts.health_figure(all_stations, all_sensors, mase, cov, active_by_key)
    corr = charts.correlation_figure(corr_keys, matrix)
    rows: Any = [_alert_row(a, i, offset_min) for i, a in enumerate(feed)] or html.P(
        "No alerts for this selection.", className="alert-empty"
    )
    note = (
        f"{n_active} active · latest first · click to inspect"
        if n_active
        else "Nothing active · latest first"
    )
    return health, corr, rows, note, digest


# ---- app ---------------------------------------------------------------------


def _parse_label(label: str) -> Key:
    station, sensor = label.split(" · S")
    return station, int(sensor)


def create_app(settings: Settings, store: DataStore, health: Any = None) -> Dash:
    app = Dash(__name__, title="SignalGrid", update_title="")
    app.layout = build_layout(settings)

    @app.server.get("/health")  # type: ignore[untyped-decorator]
    def _health() -> tuple[dict[str, Any], int]:
        body: dict[str, Any] = {"status": "ok", "service": "frontend", **store.counts()}
        if health is not None:
            body["nats_connected"] = bool(health())
        return body, 200

    # Segmented controls run in the browser (no server round trip). A click
    # writes the store and the store drives the pressed state, so drill-down
    # interactions that set the store from the server update the buttons too.
    for group, store_id in (("view", "view-mode"), ("range", "time-range"), ("scale", "scale")):
        app.clientside_callback(  # type: ignore[no-untyped-call]
            """
            function (_) {
                const t = window.dash_clientside.callback_context.triggered_id;
                return (t && t.value) ? t.value : window.dash_clientside.no_update;
            }
            """,
            Output(store_id, "data", allow_duplicate=True),
            Input({"type": f"seg-{group}", "value": ALL}, "n_clicks"),
            prevent_initial_call=True,
        )
        app.clientside_callback(  # type: ignore[no-untyped-call]
            """
            function (value, ids) {
                const on = ids.map(i => i.value === value);
                return [on.map(o => o ? "seg-btn selected" : "seg-btn"), on.map(o => o ? "true" : "false")];
            }
            """,
            Output({"type": f"seg-{group}", "value": ALL}, "className"),
            Output({"type": f"seg-{group}", "value": ALL}, "aria-pressed"),
            Input(store_id, "data"),
            State({"type": f"seg-{group}", "value": ALL}, "id"),
        )

    app.clientside_callback(  # type: ignore[no-untyped-call]
        """function (view) { return "control control-seg" + (view === "compare" ? "" : " hidden"); }""",
        Output("scale-control", "className"),
        Input("view-mode", "data"),
    )

    @app.callback(
        Output("station-dropdown", "options"),
        Output("station-dropdown", "value"),
        Output("station-dropdown", "multi"),
        Output("sensor-dropdown", "options"),
        Output("sensor-dropdown", "value"),
        Output("sensor-dropdown", "multi"),
        Input("view-mode", "data"),
        Input("discovery", "n_intervals"),
        State("station-dropdown", "value"),
        State("sensor-dropdown", "value"),
        State("station-dropdown", "options"),
        State("sensor-dropdown", "options"),
    )
    def _selection(
        view: str, _: int, station: Any, sensor: Any, st_opts: Any, se_opts: Any
    ) -> tuple[Any, ...]:
        keys = store.keys()
        stations = store.stations()
        sensors = sorted({k[1] for k in keys})
        view_changed = ctx.triggered_id == "view-mode"
        st_val = coerce_selection(view, station, stations, default_all=view == "aggregate" and view_changed)
        se_val = coerce_selection(view, sensor, sensors, default_all=view != "single" and view_changed)
        se_options = [{"label": f"Sensor {s}", "value": s} for s in sensors]
        return (
            no_update if st_opts == stations else stations,
            no_update if st_val == station else st_val,
            view != "single",
            no_update if se_opts == se_options else se_options,
            no_update if se_val == sensor else se_val,
            view != "single",
        )

    @app.callback(
        *[Output(g, "figure") for g in TIME_GRAPHS],
        *[Output(g, "extendData") for g in TIME_GRAPHS],
        Output("chart-cursor", "data"),
        *[Output(tile_id, "children") for tile_id, _, _ in KPI_TILES],
        Output("kpi-coverage-note", "children"),
        Output("kpi-alerts-note", "children"),
        Output("main-title", "children"),
        Output("main-note", "children"),
        Input("interval", "n_intervals"),
        Input("view-mode", "data"),
        Input("station-dropdown", "value"),
        Input("sensor-dropdown", "value"),
        Input("time-range", "data"),
        Input("scale", "data"),
        State("chart-cursor", "data"),
        # Never let refreshes overlap: a request that fires while another is
        # in flight would carry a stale cursor and force a full re-render,
        # which is how a slow render snowballs into seconds of lag.
        running=[(Output("interval", "disabled"), True, False)],
    )
    def _refresh(
        _: int, view: str, stations: Any, sensors: Any, range_key: str, scale: str, cursor: Any
    ) -> tuple[Any, ...]:
        figures, extends, new_cursor, kpis, titles = refresh(
            store, view, stations, sensors, range_key, scale, cursor
        )
        return (*figures, *extends, new_cursor, *kpis, *titles)

    @app.callback(
        Output("health-graph", "figure"),
        Output("corr-graph", "figure"),
        Output("alert-feed", "children"),
        Output("alerts-note", "children"),
        Output("slow-hash", "data"),
        Input("discovery", "n_intervals"),
        Input("view-mode", "data"),
        Input("station-dropdown", "value"),
        Input("sensor-dropdown", "value"),
        Input("time-range", "data"),
        Input("tz-offset", "data"),
        State("slow-hash", "data"),
    )
    def _slow(
        _: int, view: str, stations: Any, sensors: Any, range_key: str, tz: Any, digest: Any
    ) -> tuple[Any, ...]:
        return slow_refresh(store, view, stations, sensors, range_key, digest, offset_min=int(tz or 0))

    # The browser's UTC offset, so server-rendered times match the charts.
    app.clientside_callback(  # type: ignore[no-untyped-call]
        "function (_) { return -new Date().getTimezoneOffset(); }",
        Output("tz-offset", "data"),
        Input("tz-offset", "id"),
    )

    # ---- drill-down interactions --------------------------------------------

    drill = (
        Output("view-mode", "data", allow_duplicate=True),
        Output("station-dropdown", "value", allow_duplicate=True),
        Output("sensor-dropdown", "value", allow_duplicate=True),
    )

    @app.callback(*drill, Input("health-graph", "clickData"), prevent_initial_call=True)
    def _health_click(click: Any) -> tuple[Any, ...]:
        if not click:
            return no_update, no_update, no_update
        point = click["points"][0]
        return "single", point["y"], int(str(point["x"]).split()[-1])

    @app.callback(*drill, Input("corr-graph", "clickData"), prevent_initial_call=True)
    def _corr_click(click: Any) -> tuple[Any, ...]:
        if not click:
            return no_update, no_update, no_update
        point = click["points"][0]
        pair = [_parse_label(point["x"]), _parse_label(point["y"])]
        return "compare", sorted({p[0] for p in pair}), sorted({p[1] for p in pair})

    @app.callback(
        *drill,
        Input({"type": "alert-row", "index": ALL, "station": ALL, "sensor": ALL}, "n_clicks"),
        State("sensor-dropdown", "value"),
        prevent_initial_call=True,
    )
    def _alert_click(clicks: list[int | None], current_sensor: Any) -> tuple[Any, ...]:
        trigger = ctx.triggered_id
        if not isinstance(trigger, dict) or not any(c for c in clicks if c):
            return no_update, no_update, no_update  # rows were re-rendered, not clicked
        sensor = trigger["sensor"]
        if sensor < 0:
            current = _as_list(current_sensor)
            sensor = current[0] if current else 0
        return "single", trigger["station"], sensor

    return app
