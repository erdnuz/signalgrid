"""Pure figure construction (no Dash, no I/O), so it is unit-testable.

Colours are the dark steps of a CVD-validated categorical palette (slots 1-3
pass all-pairs checks on the #1a1a19 surface). Identity is never colour-only:
every series is named in the legend.
"""

from __future__ import annotations

from datetime import UTC, datetime

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .store import RegimeView, SeriesView

SURFACE = "#1a1a19"
GRID = "#2c2c2a"
AXIS = "#383835"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"

ACTUAL = "#3987e5"  # slot 1 blue
FORECAST = "#d95926"  # slot 2 orange
FORECAST_BAND = "rgba(217, 89, 38, 0.18)"
DETECTED = "#199e70"  # slot 3 aqua
TRUE_REGIME = INK_SECONDARY

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
REGIME_LABELS = ["R0", "R1", "R2"]


def _dt(ms: list[int]) -> list[datetime]:
    return [datetime.fromtimestamp(t / 1000, tz=UTC) for t in ms]


def _axis(**extra: object) -> dict[str, object]:
    return dict(
        showgrid=True,
        gridcolor=GRID,
        gridwidth=1,
        zeroline=False,
        showline=True,
        linecolor=AXIS,
        tickfont=dict(color=INK_MUTED, size=12),
        title_font=dict(color=INK_SECONDARY, size=12),
        **extra,
    )


def empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK_SECONDARY),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[dict(text=message, showarrow=False, font=dict(size=16, color=INK_MUTED))],
        margin=dict(l=40, r=24, t=24, b=40),
    )
    return fig


def build_figure(series: SeriesView | None, regimes: RegimeView | None) -> go.Figure:
    if series is None or not series.timestamps:
        return empty_figure("Waiting for data…")

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.74, 0.26],
        vertical_spacing=0.06,
    )

    if series.forecast_ts:
        x = _dt(series.forecast_ts)
        level = f"{series.confidence:.0%} interval" if series.confidence else "Interval"
        fig.add_trace(
            go.Scatter(
                x=x,
                y=series.upper_ci,
                mode="lines",
                line=dict(width=0),
                hoverinfo="skip",
                showlegend=False,
                legendgroup="band",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=series.lower_ci,
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor=FORECAST_BAND,
                name=level,
                legendgroup="band",
                hovertemplate="%{y:.3f}<extra>lower</extra>",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=series.forecasts,
                mode="lines",
                line=dict(color=FORECAST, width=2, dash="dash"),
                name="Forecast",
                hovertemplate="%{y:.3f}<extra>forecast</extra>",
            ),
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=_dt(series.timestamps),
            y=series.actuals,
            mode="lines",
            line=dict(color=ACTUAL, width=2),
            name="Actual (window mean)",
            hovertemplate="%{y:.3f}<extra>actual</extra>",
        ),
        row=1,
        col=1,
    )

    if regimes is not None and regimes.timestamps:
        rx = _dt(regimes.timestamps)
        fig.add_trace(
            go.Scatter(
                x=rx,
                y=regimes.true_regime,
                mode="lines",
                line=dict(color=TRUE_REGIME, width=2, shape="hv"),
                name="True regime",
                hovertemplate="R%{y}<extra>true</extra>",
            ),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=rx,
                y=regimes.detected,
                mode="lines",
                line=dict(color=DETECTED, width=2, shape="hv", dash="dot"),
                name="Detected regime",
                hovertemplate="R%{y}<extra>detected</extra>",
            ),
            row=2,
            col=1,
        )

    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#262624", bordercolor=AXIS, font=dict(color=INK, family=FONT)),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(color=INK_SECONDARY, size=12),
            bgcolor="rgba(0,0,0,0)",
        ),
        margin=dict(l=56, r=24, t=48, b=40),
        # Keep zoom/pan while the figure refreshes, reset when the series changes.
        uirevision=f"{series.station}/{series.sensor}",
    )
    fig.update_xaxes(_axis())
    fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    fig.update_yaxes(_axis(title_text="Value"), row=1, col=1)
    fig.update_yaxes(
        _axis(
            title_text="Regime",
            tickvals=[0, 1, 2],
            ticktext=REGIME_LABELS,
            range=[-0.4, 2.4],
            fixedrange=True,
        ),
        row=2,
        col=1,
    )
    return fig
