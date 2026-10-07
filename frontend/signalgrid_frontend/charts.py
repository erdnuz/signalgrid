"""Chart construction and incremental updates (pure: no Dash, no I/O).

A time chart is a list of styled `Series`, each wrapping a `Trace` of data.
The same traces drive both

* `render_chart`: a full figure, cropped to the selected time range and
  down-sampled for long ranges, drawn only when the view, selection, range
  or data epoch changes; and
* `delta`: the points each trace gained since the client's cursor, sent as a
  Plotly `extendTraces` payload (`dcc.Graph.extendData`), so steady-state
  updates append a few points instead of redrawing the figure.

Every time chart ends with an invisible two-point *range anchor* spanning
[now - range, now]. Autorange then always shows exactly the selected window,
all panels share the same right edge, and the window keeps sliding while
only new points are sent.

x values are epoch milliseconds on `type="date"` axes (Plotly reads numbers
on date axes as UTC ms), which avoids formatting tens of thousands of date
strings per render.

Colours are the dark steps of a CVD-validated categorical palette. Colour
follows the entity: a series keeps its colour across views and selections.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Any, Literal

import numpy as np

from .store import Key, RegimeView, SeriesView

SURFACE = "#1a1a19"
GRID = "#2c2c2a"
AXIS = "#383835"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"

#: categorical slots, dark-surface steps, in their validated order
PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
ACTUAL, FORECAST, DETECTED = PALETTE[0], PALETTE[1], PALETTE[2]
FORECAST_BAND = "rgba(217, 89, 38, 0.18)"
NEUTRAL = INK_SECONDARY
#: diverging blue <-> red with a neutral grey midpoint
DIVERGING = [[0.0, "#3987e5"], [0.5, "#383835"], [1.0, "#e66767"]]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
REGIME_LABELS = ["R0", "R1", "R2"]

#: 500 ms windows: 120 / 600 / 1 800 points per series. Longer ranges would
#: need server-side roll-ups to say anything the archive API doesn't already.
RANGES_S = {"1m": 60, "5m": 300, "15m": 900}
DEFAULT_RANGE = "1m"
#: full renders are down-sampled to at most this many points per trace
MAX_RENDER_POINTS = 1_500
#: above this many points per trace, WebGL is faster than SVG
WEBGL_THRESHOLD = 1_000

Agg = Literal["mean", "min", "max", "last"]
#: a Plotly figure (or part of one) as plain JSON-able dicts
Figure = dict[str, Any]


# ---- data -----------------------------------------------------------------


@dataclass(frozen=True)
class Trace:
    x: Sequence[int]  # epoch ms, ascending
    y: Sequence[float | int | None]
    #: True for traces that show only their latest batch (the forecast
    #: horizon, the range anchor); False for append-only time series.
    replace: bool = False
    #: expected spacing of points, used to size the client-side point cap
    interval_ms: int = 500
    #: how to combine points when down-sampling
    agg: Agg = "mean"


@dataclass(frozen=True)
class Series:
    trace: Trace
    name: str
    color: str = INK
    width: float = 2
    dash: str | None = None
    shape: str = "linear"
    fill: str | None = None  # "tonexty": fill to the previous series
    fillcolor: str | None = None
    legend: bool = True
    group: str | None = None
    row: int = 1
    hover: str | None = ".3f"  # None: not in the hover label
    #: y' = (y - a) / b, fixed at render time (used for z-scores)
    affine: tuple[float, float] = (0.0, 1.0)


@dataclass(frozen=True)
class Chart:
    series: list[Series]
    height: int = 220
    rows: int = 1
    row_heights: list[float] | None = None
    #: y-axis settings per row
    yaxes: list[dict[str, Any]] = field(default_factory=lambda: [{}])
    legend: bool = False
    empty: str = "Waiting for data…"
    #: horizontal reference line on row 1: (y, label)
    reference: tuple[float, str] | None = None


def series_color(key: Key, all_keys: Sequence[Key]) -> str:
    """A series keeps its colour wherever it appears (colour follows the entity)."""
    idx = list(all_keys).index(key) if key in all_keys else 0
    return PALETTE[idx % len(PALETTE)]


def station_color(station: str, stations: Sequence[str]) -> str:
    idx = list(stations).index(station) if station in stations else 0
    return PALETTE[idx % len(PALETTE)]


def series_label(key: Key) -> str:
    return f"{key[0]} · S{key[1]}"


def _median_spacing(ts: Sequence[int], default: int) -> int:
    tail = ts[-21:]
    gaps = sorted(b - a for a, b in pairwise(tail) if b > a)
    return gaps[len(gaps) // 2] if gaps else default


def anchor(end_ms: int, range_ms: int, y: float) -> Trace:
    return Trace([end_ms - range_ms, end_ms], [y, y], replace=True)


def traces_of(chart: Chart, end_ms: int, range_ms: int) -> list[Trace]:
    """Data traces plus the range anchor, in render order."""
    traces = [s.trace for s in chart.series]
    if not any(t.x for t in traces):
        return []
    # The anchor's y must sit inside the visible data or it would stretch the
    # y-axis: use the newest value of the first series that has one.
    y0 = next((v for t in traces for v in reversed(t.y) if v is not None), 0.0)
    a, b = next((s.affine for s in chart.series if s.trace.x), (0.0, 1.0))
    return [*traces, anchor(end_ms, range_ms, (float(y0) - a) / b)]


def affines_of(chart: Chart) -> list[list[float]]:
    """Per-trace normalisation, anchor last (already normalised)."""
    return [list(s.affine) for s in chart.series] + [[0.0, 1.0]]


# ---- incremental updates ---------------------------------------------------


def cursor_for(traces: Sequence[Trace]) -> list[int | None]:
    return [(t.x[0] if t.replace else t.x[-1]) if t.x else None for t in traces]


def max_points(trace: Trace, range_ms: int) -> int:
    # Exactly one range of points: anything older is dropped client-side, so
    # off-screen history never stretches the y-axis autorange.
    return max(1, range_ms // max(trace.interval_ms, 1) + 1)


def delta(
    traces: Sequence[Trace],
    last: Sequence[int | None],
    range_ms: int,
    affine: Sequence[Sequence[float]] | None = None,
) -> tuple[list[Any] | None, list[int | None]]:
    """Points added since `last`, as a `dcc.Graph.extendData` value
    `[{"x": [...], "y": [...]}, trace_indices, max_points]`, or None if
    nothing changed (the caller then sends `no_update` and the chart is not
    touched at all)."""
    xs: list[list[int]] = []
    ys: list[list[Any]] = []
    indices: list[int] = []
    caps: list[int] = []
    new_last = list(last) + [None] * max(0, len(traces) - len(last))

    for i, trace in enumerate(traces):
        prev = new_last[i]
        if trace.replace:
            if not trace.x or trace.x[0] == prev:
                continue
            px, py, cap = list(trace.x), list(trace.y), len(trace.x)
            new_last[i] = trace.x[0]
        else:
            start = 0 if prev is None else bisect_right(trace.x, prev)
            if start >= len(trace.x):
                continue
            px, py = list(trace.x[start:]), list(trace.y[start:])
            cap = max_points(trace, range_ms)
            new_last[i] = trace.x[-1]
        if affine is not None and i < len(affine):
            a, b = affine[i]
            py = [None if v is None else (v - a) / b for v in py]
        xs.append(px)
        ys.append(py)
        indices.append(i)
        caps.append(cap)

    if not indices:
        return None, new_last
    return [{"x": xs, "y": ys}, indices, {"x": caps, "y": caps}], new_last


def crop(trace: Trace, cutoff_ms: int) -> Trace:
    if trace.replace or not trace.x:
        return trace
    start = bisect_right(trace.x, cutoff_ms - 1)
    return replace(trace, x=trace.x[start:], y=trace.y[start:])


def decimate(trace: Trace, max_n: int = MAX_RENDER_POINTS) -> Trace:
    """Bucket-aggregates a long trace for display (min/max keep band edges
    conservative, `last` keeps step lines honest)."""
    n = len(trace.x)
    if trace.replace or n <= max_n:
        return trace
    size = math.ceil(n / max_n)
    x_out: list[int] = []
    y_out: list[float | int | None] = []
    for i in range(0, n, size):
        ys = [v for v in trace.y[i : i + size] if v is not None]
        x_out.append(trace.x[min(i + size, n) - 1])
        if not ys:
            y_out.append(None)
        elif trace.agg == "max":
            y_out.append(max(ys))
        elif trace.agg == "min":
            y_out.append(min(ys))
        elif trace.agg == "last":
            y_out.append(ys[-1])
        else:
            y_out.append(sum(ys) / len(ys))
    return replace(trace, x=x_out, y=y_out)


# ---- rendering -------------------------------------------------------------


def _axis(**extra: Any) -> dict[str, Any]:
    return dict(
        showgrid=True,
        gridcolor=GRID,
        gridwidth=1,
        zeroline=False,
        showline=True,
        linecolor=AXIS,
        tickfont=dict(color=INK_MUTED, size=11),
        title_font=dict(color=INK_SECONDARY, size=12),
        **extra,
    )


def _layout(height: int, legend: bool, uirevision: str) -> Figure:
    return {
        "height": height,
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT, "color": INK},
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": "#262624", "bordercolor": AXIS, "font": {"color": INK, "family": FONT}},
        "showlegend": legend,
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"color": INK_SECONDARY, "size": 12},
            "bgcolor": "rgba(0,0,0,0)",
        },
        "margin": {"l": 56, "r": 20, "t": 40 if legend else 12, "b": 32},
        # Keeps zoom/pan across incremental updates; resets on a new selection.
        "uirevision": uirevision,
    }


def empty_figure(message: str, height: int = 220) -> Figure:
    return {
        "data": [],
        "layout": {
            "height": height,
            "paper_bgcolor": SURFACE,
            "plot_bgcolor": SURFACE,
            "font": {"family": FONT, "color": INK_SECONDARY},
            "xaxis": {"visible": False},
            "yaxis": {"visible": False},
            "annotations": [
                {
                    "text": message,
                    "showarrow": False,
                    "xref": "paper",
                    "yref": "paper",
                    "x": 0.5,
                    "y": 0.5,
                    "font": {"size": 14, "color": INK_MUTED},
                }
            ],
            "margin": {"l": 20, "r": 20, "t": 20, "b": 20},
        },
    }


def render_chart(chart: Chart, end_ms: int, range_ms: int, uirevision: str) -> Figure:
    """Builds the figure as plain dicts. Dash accepts them directly, which
    skips plotly.graph_objects' per-point validation (the dominant cost of
    a full render)."""
    traces = traces_of(chart, end_ms, range_ms)
    if not traces:
        return empty_figure(chart.empty, chart.height)

    cutoff = end_ms - range_ms
    shown = [decimate(crop(t, cutoff)) for t in traces]
    kind = "scattergl" if max(len(t.x) for t in shown) > WEBGL_THRESHOLD else "scatter"

    def axes(row: int) -> dict[str, str]:
        return {"xaxis": "x" if row == 1 else f"x{row}", "yaxis": "y" if row == 1 else f"y{row}"}

    data: list[Figure] = []
    for s, t in zip(chart.series, shown, strict=False):
        a, b = s.affine
        trace: Figure = {
            "type": kind,
            "x": list(t.x),
            "y": [None if v is None else (v - a) / b for v in t.y],
            "mode": "lines",
            "name": s.name,
            "legendgroup": s.group or s.name,
            "showlegend": chart.legend and s.legend,
            "line": {"color": s.color, "width": s.width, "dash": s.dash or "solid", "shape": s.shape},
            **axes(s.row),
        }
        if s.fill:
            trace.update(fill=s.fill, fillcolor=s.fillcolor)
        if s.hover is None:
            trace["hoverinfo"] = "skip"
        else:
            trace["hovertemplate"] = f"%{{y:{s.hover}}}"
        data.append(trace)

    # Range anchor: invisible, never hovered, last in the trace list.
    data.append(
        {
            "type": kind,
            "x": list(shown[-1].x),
            "y": list(shown[-1].y),
            "mode": "markers",
            "marker": {"opacity": 0, "size": 1},
            "hoverinfo": "skip",
            "showlegend": False,
            **axes(1),
        }
    )

    layout = _layout(chart.height, chart.legend, uirevision)
    heights = chart.row_heights or [1.0 / chart.rows] * chart.rows
    gap = 0.06 if chart.rows > 1 else 0.0
    usable = 1.0 - gap * (chart.rows - 1)
    top = 1.0
    for row in range(1, chart.rows + 1):
        h = heights[row - 1] / sum(heights) * usable
        domain = [max(top - h, 0.0), top]
        top = domain[0] - gap
        suffix = "" if row == 1 else str(row)
        bottom_row = row == chart.rows
        xaxis = _axis(type="date", anchor=f"y{suffix}", showticklabels=bottom_row)
        if chart.rows > 1 and not bottom_row:
            xaxis["matches"] = f"x{chart.rows}"
        layout[f"xaxis{suffix}"] = xaxis
        settings = dict(chart.yaxes[row - 1]) if row - 1 < len(chart.yaxes) else {}
        title = settings.pop("title_text", None)
        y = _axis(**settings)
        title_font = y.pop("title_font")
        if title:
            y["title"] = {"text": title, "font": title_font}
        y.update(domain=domain, anchor=f"x{suffix}")
        layout[f"yaxis{suffix}"] = y

    if chart.reference is not None:
        value, label = chart.reference
        layout["shapes"] = [
            {
                "type": "line",
                "xref": "x domain",
                "x0": 0,
                "x1": 1,
                "yref": "y",
                "y0": value,
                "y1": value,
                "line": {"color": INK_MUTED, "width": 1, "dash": "dash"},
            }
        ]
        layout["annotations"] = [
            {
                "text": label,
                "xref": "x domain",
                "x": 1,
                "xanchor": "right",
                "yref": "y",
                "y": value,
                "yanchor": "top",
                "showarrow": False,
                "font": {"color": INK_MUTED, "size": 11},
            }
        ]
    return {"data": data, "layout": layout}


# ---- chart builders --------------------------------------------------------


def detail_chart(series: SeriesView | None, regimes: RegimeView | None) -> Chart:
    """Single series: actual, 1-step forecast history + latest horizon with
    the prediction band, and the regime strip (true vs. detected)."""
    if series is None:
        return Chart([], height=620)
    w = series.window_ms
    level = f"{series.confidence:.0%} interval" if series.confidence else "Interval"
    r_ts = regimes.timestamps if regimes else []
    r_int = _median_spacing(r_ts, w)
    band: dict[str, Any] = dict(width=0, group="band", hover=None)
    return Chart(
        [
            Series(
                Trace(series.one_step_ts, series.one_step_hi, interval_ms=w, agg="max"),
                "band-hi",
                legend=False,
                **band,
            ),
            Series(
                Trace(series.one_step_ts, series.one_step_lo, interval_ms=w, agg="min"),
                level,
                fill="tonexty",
                fillcolor=FORECAST_BAND,
                **band,
            ),
            Series(Trace(series.horizon_ts, series.horizon_hi, replace=True), "hz-hi", legend=False, **band),
            Series(
                Trace(series.horizon_ts, series.horizon_lo, replace=True),
                "hz-lo",
                legend=False,
                fill="tonexty",
                fillcolor=FORECAST_BAND,
                **band,
            ),
            Series(
                Trace(series.one_step_ts, series.one_step, interval_ms=w),
                "Forecast (1 step ahead)",
                color=FORECAST,
                dash="dash",
                group="forecast",
            ),
            Series(
                Trace(series.horizon_ts, series.horizon, replace=True),
                "Forecast horizon",
                color=FORECAST,
                dash="dash",
                group="forecast",
                legend=False,
            ),
            Series(
                Trace(series.timestamps, series.actuals, interval_ms=w), "Actual (window mean)", color=ACTUAL
            ),
            Series(
                Trace(r_ts, regimes.true_regime if regimes else [], interval_ms=r_int, agg="last"),
                "True regime",
                color=NEUTRAL,
                shape="hv",
                row=2,
                hover=".0f",
            ),
            Series(
                Trace(r_ts, regimes.detected if regimes else [], interval_ms=r_int, agg="last"),
                "Detected regime",
                color=DETECTED,
                shape="hv",
                dash="dot",
                row=2,
                hover=".0f",
            ),
        ],
        height=620,
        rows=2,
        row_heights=[0.74, 0.26],
        yaxes=[
            dict(title_text="Value"),
            dict(
                title_text="Regime",
                tickvals=[0, 1, 2],
                ticktext=REGIME_LABELS,
                range=[-0.4, 2.4],
                fixedrange=True,
            ),
        ],
        legend=True,
    )


def zscore(values: Sequence[float | None]) -> tuple[float, float]:
    vals = np.array([v for v in values if v is not None], dtype=float)
    if len(vals) < 2:
        return 0.0, 1.0
    sd = float(vals.std())
    return float(vals.mean()), sd if sd > 1e-12 else 1.0


def compare_chart(
    views: Sequence[SeriesView], all_keys: Sequence[Key], normalize: bool, cutoff_ms: int
) -> Chart:
    """Overlay of several series' window means, optionally z-scored over the
    visible range so series with different levels can be compared by shape."""
    series = []
    for v in views:
        key = (v.station, v.sensor)
        start = bisect_right(v.timestamps, cutoff_ms - 1)
        affine = zscore(v.actuals[start:]) if normalize else (0.0, 1.0)
        series.append(
            Series(
                Trace(v.timestamps, v.actuals, interval_ms=v.window_ms),
                series_label(key),
                color=series_color(key, all_keys),
                affine=affine,
            )
        )
    return Chart(
        series,
        height=520,
        yaxes=[dict(title_text="z-score (visible range)" if normalize else "Value")],
        legend=True,
        empty="Select at least one station and sensor",
    )


def cross_section(views: Sequence[SeriesView]) -> tuple[list[int], list[float], list[float], list[float]]:
    """Aligns several series on their common window timestamps."""
    if not views:
        return [], [], [], []
    maps = [dict(zip(v.timestamps, v.actuals, strict=True)) for v in views]
    common = sorted(set(maps[0]).intersection(*maps[1:]))
    rows = [[m[t] for m in maps] for t in common]
    return common, [sum(r) / len(r) for r in rows], [min(r) for r in rows], [max(r) for r in rows]


def _alpha(hex_color: str, alpha: float) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def aggregate_chart(groups: Mapping[str, Sequence[SeriesView]], stations: Sequence[str]) -> Chart:
    """Per station: the mean of the selected sensors' window means, with the
    min-max envelope across those sensors."""
    series: list[Series] = []
    for station, views in groups.items():
        ts, mean, lo, hi = cross_section(views)
        if not ts:
            continue
        color = station_color(station, stations)
        w = views[0].window_ms
        series += [
            Series(
                Trace(ts, hi, interval_ms=w, agg="max"),
                f"{station} max",
                width=0,
                legend=False,
                hover=None,
                group=station,
            ),
            Series(
                Trace(ts, lo, interval_ms=w, agg="min"),
                f"{station} range",
                width=0,
                fill="tonexty",
                fillcolor=_alpha(color, 0.16),
                legend=False,
                hover=None,
                group=station,
            ),
            Series(
                Trace(ts, mean, interval_ms=w),
                f"{station} · mean of {len(views)} sensor{'s' if len(views) > 1 else ''}",
                color=color,
                group=station,
            ),
        ]
    return Chart(
        series, height=520, yaxes=[dict(title_text="Value")], legend=True, empty="Select at least one station"
    )


def metric_chart(
    lines: Sequence[tuple[str, str, Trace]],
    *,
    y_title: str,
    y_format: str,
    empty: str,
    y_range: tuple[float, float] | None = None,
    include: list[float] | None = None,
    reference: tuple[float, str] | None = None,
) -> Chart:
    """A small multi-line panel; with one line the panel title names it."""
    yaxis: dict[str, Any] = dict(title_text=y_title, tickformat=y_format)
    if y_range is not None:
        yaxis["range"] = list(y_range)
    elif include is not None:
        # Autorange around the data, but always keep the reference in view.
        yaxis["autorangeoptions"] = dict(include=include)
    else:
        yaxis["rangemode"] = "tozero"
    return Chart(
        [Series(t, name, color=color, hover=y_format) for name, color, t in lines],
        yaxes=[yaxis],
        legend=len(lines) > 1,
        empty=empty,
        reference=reference,
    )


# ---- matrix views ------------------------------------------------------------


def health_figure(
    stations: Sequence[str],
    sensors: Sequence[int],
    mase: dict[Key, float | None],
    coverage: dict[Key, float | None],
    alerts: dict[Key, int],
) -> Figure:
    """Station x sensor grid coloured by MASE (blue: beats persistence, red:
    worse), each cell labelled with its numbers. Click a cell to drill in."""
    if not stations or not sensors:
        return empty_figure("Waiting for data\u2026", 260)
    z: list[list[float | None]] = []
    text: list[list[str]] = []
    for st in stations:
        z_row, t_row = [], []
        for s in sensors:
            m, c, n = mase.get((st, s)), coverage.get((st, s)), alerts.get((st, s), 0)
            z_row.append(m)
            parts = [
                f"MASE {m:.2f}" if m is not None else "MASE \u2014",
                f"cov {c:.0%}" if c is not None else "cov \u2014",
            ]
            if n:
                plural = "s" if n > 1 else ""
                parts.append(f"\u25b2 {n} alert{plural}")
            t_row.append("<br>".join(parts))
        z.append(z_row)
        text.append(t_row)
    heatmap = {
        "type": "heatmap",
        "z": z,
        "x": [f"Sensor {s}" for s in sensors],
        "y": list(stations),
        "text": text,
        "texttemplate": "%{text}",
        "textfont": {"color": INK, "size": 12},
        "colorscale": DIVERGING,
        "zmin": 0.8,
        "zmid": 1.0,
        "zmax": 1.2,
        "xgap": 2,
        "ygap": 2,
        "hovertemplate": "%{y} \u00b7 %{x}<br>%{text}<extra>click to inspect</extra>",
        "colorbar": _colorbar("MASE"),
    }
    return {"data": [heatmap], "layout": _matrix_layout(260)}


def correlation(
    views: Sequence[SeriesView], cutoff_ms: int, min_points: int = 20
) -> tuple[list[Key], list[list[float | None]]]:
    """Pearson correlation of window-to-window *changes* over the visible
    range. Levels of mean-reverting signals are strongly autocorrelated,
    which inflates level correlations; changes show the co-movement the
    simulator actually injects (regime-specific cross-channel shocks)."""
    keys = [(v.station, v.sensor) for v in views]
    maps = [{t: a for t, a in zip(v.timestamps, v.actuals, strict=True) if t >= cutoff_ms} for v in views]
    n = len(views)
    out: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            common = sorted(set(maps[i]) & set(maps[j]))
            if len(common) < min_points + 1:
                continue
            a = np.diff([maps[i][t] for t in common])
            b = np.diff([maps[j][t] for t in common])
            if a.std() < 1e-12 or b.std() < 1e-12:
                continue
            r = float(np.corrcoef(a, b)[0, 1])
            out[i][j] = out[j][i] = round(r, 2)
    return keys, out


def correlation_figure(keys: Sequence[Key], matrix: Sequence[Sequence[float | None]]) -> Figure:
    if len(keys) < 2:
        return empty_figure("Needs at least two series", 360)
    labels = [series_label(k) for k in keys]
    text = [["" if v is None else f"{v:+.2f}" for v in row] for row in matrix]
    heatmap = {
        "type": "heatmap",
        "z": [list(r) for r in matrix],
        "x": labels,
        "y": labels,
        "text": text,
        "texttemplate": "%{text}",
        "textfont": {"color": INK, "size": 10},
        "colorscale": DIVERGING,
        "zmin": -1,
        "zmid": 0,
        "zmax": 1,
        "xgap": 2,
        "ygap": 2,
        "hovertemplate": "%{y} vs %{x}: %{z:+.2f}<extra>click to compare</extra>",
        "colorbar": _colorbar("r"),
    }
    layout = _matrix_layout(360)
    layout["yaxis"]["autorange"] = "reversed"
    return {"data": [heatmap], "layout": layout}


def _colorbar(title: str) -> Figure:
    return {
        "title": {"text": title, "font": {"color": INK_SECONDARY, "size": 11}},
        "tickfont": {"color": INK_MUTED, "size": 10},
        "thickness": 10,
        "len": 0.9,
    }


def _matrix_layout(height: int) -> Figure:
    ticks = {"tickfont": {"color": INK_SECONDARY, "size": 11}, "showgrid": False, "zeroline": False}
    return {
        "height": height,
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT, "color": INK},
        "margin": {"l": 96, "r": 16, "t": 12, "b": 56},
        "hoverlabel": {"bgcolor": "#262624", "bordercolor": AXIS, "font": {"color": INK, "family": FONT}},
        "xaxis": {**ticks, "side": "bottom"},
        "yaxis": dict(ticks),
    }
