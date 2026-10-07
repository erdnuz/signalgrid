from signalgrid_forecast.evaluation import ForecastEvaluator, Prediction

KEY = ("StationA", 0)


def test_scores_prediction_when_actual_arrives():
    ev = ForecastEvaluator()
    ev.register(KEY, 1, 1_000, Prediction(point=1.0, lower=0.5, upper=1.5, origin_value=0.8))
    (score,) = ev.observe(KEY, 1_000, 1.2)
    assert abs(score.abs_error - 0.2) < 1e-12
    assert abs(score.naive_abs_error - 0.4) < 1e-12  # persistence predicted 0.8
    assert score.covered
    m = ev.horizon_metrics(KEY, 1)
    assert m.n == 1 and m.coverage == 1.0 and abs(m.mase - 0.5) < 1e-12


def test_horizons_are_scored_independently():
    ev = ForecastEvaluator(horizons=(1, 3))
    ev.register(KEY, 1, 500, Prediction(1.0, 0.0, 2.0, origin_value=1.0))
    ev.register(KEY, 3, 1_500, Prediction(1.0, 0.9, 1.1, origin_value=1.0))
    assert [s.horizon for s in ev.observe(KEY, 500, 1.0)] == [1]
    ev.observe(KEY, 1_000, 1.0)
    (three,) = ev.observe(KEY, 1_500, 3.0)
    assert three.horizon == 3 and not three.covered
    m1, m3 = ev.metrics(KEY)
    assert (m1.horizon, m1.n, m3.horizon, m3.n) == (1, 1, 3, 1)


def test_actual_outside_interval_is_not_covered():
    ev = ForecastEvaluator()
    ev.register(KEY, 1, 1_000, Prediction(point=1.0, lower=0.9, upper=1.1))
    (score,) = ev.observe(KEY, 1_000, 2.0)
    assert not score.covered


def test_unmatched_and_stale_predictions_are_ignored():
    ev = ForecastEvaluator()
    ev.register(KEY, 1, 1_000, Prediction(1.0, 0.0, 2.0))
    assert ev.observe(KEY, 1_500, 1.0) == []  # skipped window: stale prediction dropped
    assert ev.observe(KEY, 1_000, 1.0) == []
    assert ev.horizon_metrics(KEY, 1).n == 0


def test_mase_is_scale_free_and_safe_near_zero():
    # Zero-centred series: MAPE would divide by ~0; MASE stays finite.
    ev = ForecastEvaluator()
    for ts, (actual, origin) in enumerate([(0.001, -0.001), (-0.002, 0.001), (0.0, -0.002)]):
        ev.register(KEY, 1, ts, Prediction(0.0, -1.0, 1.0, origin_value=origin))
        ev.observe(KEY, ts, actual)
    m = ev.horizon_metrics(KEY, 1)
    assert m.mase is not None and 0 < m.mase < 10


def test_rolling_window_bounds_history():
    ev = ForecastEvaluator(window=3)
    for ts in range(10):
        ev.register(KEY, 1, ts, Prediction(0.0, -1.0, 1.0))
        ev.observe(KEY, ts, 0.5)
    assert ev.horizon_metrics(KEY, 1).n == 3
