from __future__ import annotations

from telemetry import BoundedHistogram


def test_histogram_snapshot_uses_fixed_non_cumulative_buckets() -> None:
    histogram = BoundedHistogram("test_seconds", "test", (0.1, 1.0), max_series=4)

    histogram.observe(0.05, model="m1")
    histogram.observe(0.5, model="m1")
    histogram.observe(2.0, model="m1")
    histogram.observe(-1.0, model="m1")

    series = histogram.snapshot().series
    assert len(series) == 1
    assert series[0].buckets == (1, 1)
    assert series[0].count == 3
    assert series[0].total == 2.55


def test_histogram_cardinality_is_strictly_bounded() -> None:
    histogram = BoundedHistogram("test_seconds", "test", (1.0,), max_series=2)

    histogram.observe(0.1, model="m1", node="n1", outcome="success")
    histogram.observe(0.2, model="m2", node="n2", outcome="error")
    histogram.observe(0.3, model="m3", node="n3", outcome="timeout")

    series = histogram.snapshot().series
    assert len(series) == 2
    overflow = next(item for item in series if item.model == "__overflow__")
    assert overflow.node == "__overflow__"
    assert overflow.outcome == "other"
    assert overflow.count == 2


def test_histogram_normalizes_labels_and_can_reset() -> None:
    histogram = BoundedHistogram("test_seconds", "test", (1.0,))
    histogram.observe(0.1, model="", node="", outcome="unexpected")

    series = histogram.snapshot().series[0]
    assert (series.model, series.node, series.outcome) == ("unknown", "local", "other")

    histogram.reset()
    assert histogram.snapshot().series == ()
