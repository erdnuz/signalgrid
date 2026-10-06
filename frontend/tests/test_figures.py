from helpers import forecast, regime, stats
from signalgrid_frontend.figures import SURFACE, build_figure


def test_empty_state_has_message_and_dark_surface():
    fig = build_figure(None, None)
    assert fig.layout.paper_bgcolor == SURFACE
    assert "Waiting" in fig.layout.annotations[0].text


def test_one_trace_per_series_with_named_legend(store):
    for i in range(5):
        store.add_stats(stats(i * 500))
    store.add_forecast(forecast(2_000))
    store.add_regime(regime(0, 0, 0))
    fig = build_figure(store.series("StationA", 0), store.regimes("StationA"))
    names = [t.name for t in fig.data if t.showlegend is not False]
    # Regression: a duplicated loop used to add each series N^2 times.
    assert names.count("Actual (window mean)") == 1
    assert {"Forecast", "98% interval", "True regime", "Detected regime"} <= set(names)
    assert fig.layout.paper_bgcolor == SURFACE


def test_layout_applied_without_forecasts(store):
    # Regression: layout used to be applied only once a forecast existed.
    store.add_stats(stats(0))
    fig = build_figure(store.series("StationA", 0), None)
    assert fig.layout.plot_bgcolor == SURFACE
    assert len(fig.data) == 1
