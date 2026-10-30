"""Metric arithmetic and the guarantees run reports rely on."""

import time

from src.loaders.base import LoadCounts, row_hashes, serialize_column
from src.monitoring.metrics import RunMetrics

import pandas as pd


def test_failure_rate_and_throughput() -> None:
    metrics = RunMetrics(total_seconds=2.0)
    metrics.table("a").input = 90
    metrics.table("a").rejected = 5
    metrics.table("b").input = 10
    metrics.table("b").rejected = 5

    assert metrics.total("input") == 100
    assert metrics.failure_rate == 0.1
    assert metrics.throughput == 50.0


def test_rates_are_zero_not_an_error_for_an_empty_run() -> None:
    metrics = RunMetrics()

    assert metrics.failure_rate == 0.0
    assert metrics.throughput == 0.0


def test_stage_timer_accumulates_and_survives_exceptions() -> None:
    metrics = RunMetrics()

    with metrics.timed("loading"):
        time.sleep(0.01)
    try:
        with metrics.timed("loading"):
            raise ValueError
    except ValueError:
        pass

    assert metrics.stage_seconds["loading"] >= 0.01


def test_rollback_reports_nothing_published_and_everything_failed() -> None:
    metrics = RunMetrics()
    metrics.table("a").valid = 10
    metrics.table("a").load = LoadCounts(inserted=10)

    metrics.mark_rolled_back()

    assert metrics.load_total("inserted") == 0
    assert metrics.total("failed") == 10


def test_as_rows_includes_rule_counts_and_run_level_values() -> None:
    metrics = RunMetrics(total_seconds=1.0)
    metrics.table("a").input = 2
    metrics.table("a").rule_failures["bad"] = 2

    rows = {(r["table_name"], r["metric_name"]): r["metric_value"] for r in metrics.as_rows()}

    assert rows[("a", "rule:bad")] == 2
    assert rows[("a", "input")] == 2
    assert rows[("_run", "throughput_rows_per_second")] == 2.0


def test_source_total_is_summed_without_float_drift() -> None:
    metrics = RunMetrics()
    table = metrics.table("a")
    for _ in range(10):
        table.add_source_value("x", 0.1)

    assert table.aggregate_total("x") == 1.0


def test_row_hash_changes_with_content_and_ignores_excluded_columns() -> None:
    base = {"a": ["1"], "b": ["x"], "count": ["5"]}

    original = row_hashes(base, {"count"})

    assert row_hashes({**base, "count": ["6"]}, {"count"}) == original
    assert row_hashes({**base, "b": ["y"]}, {"count"}) != original


def test_row_hash_distinguishes_null_from_empty_and_shifted_values() -> None:
    null = row_hashes({"a": [None], "b": ["x"]}, set())
    empty = row_hashes({"a": [""], "b": ["x"]}, set())
    assert null != empty

    assert row_hashes({"a": ["ab"], "b": ["c"]}, set()) != row_hashes({"a": ["a"], "b": ["bc"]}, set())


def test_serialization_renders_postgres_text_input() -> None:
    assert serialize_column(pd.Series([1, None], dtype="Int64"), "integer") == ["1", None]
    assert serialize_column(pd.Series([1.5, None]), "decimal") == ["1.5", None]
    assert serialize_column(pd.to_datetime(pd.Series(["2020-01-02 03:04:05", None])), "timestamp") == ["2020-01-02T03:04:05", None]
    assert serialize_column(pd.to_datetime(pd.Series(["2020-01-02"])), "date") == ["2020-01-02"]
    assert serialize_column(pd.Series(["a", None], dtype=object), "text") == ["a", None]
