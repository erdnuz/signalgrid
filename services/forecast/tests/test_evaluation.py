from signalgrid_forecast.evaluation import ForecastEvaluator, Prediction

KEY = ("StationA", 0)


def test_scores_prediction_when_actual_arrives():
    ev = ForecastEvaluator()
    ev.register(KEY, 1_000, Prediction(point=1.0, lower=0.5, upper=1.5))
    score = ev.observe(KEY, 1_000, 1.2)
    assert score is not None
    assert abs(score.abs_error - 0.2) < 1e-12
    assert score.covered
    m = ev.metrics(KEY)
    assert m.n == 1 and m.coverage == 1.0


def test_actual_outside_interval_is_not_covered():
    ev = ForecastEvaluator()
    ev.register(KEY, 1_000, Prediction(point=1.0, lower=0.9, upper=1.1))
    assert not ev.observe(KEY, 1_000, 2.0).covered


def test_unmatched_and_stale_predictions_are_ignored():
    ev = ForecastEvaluator()
    ev.register(KEY, 1_000, Prediction(1.0, 0.0, 2.0))
    assert ev.observe(KEY, 1_500, 1.0) is None  # skipped window: stale prediction dropped
    assert ev.observe(KEY, 1_000, 1.0) is None
    assert ev.metrics(KEY).n == 0


def test_rolling_window_bounds_history():
    ev = ForecastEvaluator(window=3)
    for ts in range(10):
        ev.register(KEY, ts, Prediction(0.0, -1.0, 1.0))
        ev.observe(KEY, ts, 0.5)
    assert ev.metrics(KEY).n == 3
    assert ev.overall().n == 3
