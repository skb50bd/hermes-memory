"""TDD: PgMetricsRepo — record events, time-bucketed percentile query."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def metrics_repo(pg_conn):
    from hermes_memory.pg_repos import PgMetricsRepo

    return PgMetricsRepo(pg_conn)


def test_record_and_query(metrics_repo) -> None:
    for v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        metrics_repo.record("default", "mcp.tool.duration_ms", float(v))
    points = metrics_repo.query("default", "mcp.tool.duration_ms", bucket="1 minute")
    assert len(points) >= 1
    p = points[0]
    assert p.count == 10
    assert p.p50 == 55.0
    assert p.p95 >= 90.0


def test_query_filters_by_profile(metrics_repo) -> None:
    metrics_repo.record("default", "metric", 1.0)
    metrics_repo.record("other", "metric", 1.0)
    points = metrics_repo.query("default", "metric", bucket="1 minute")
    assert all(p.count == 1 for p in points)


def test_query_filters_by_name(metrics_repo) -> None:
    metrics_repo.record("default", "metric_a", 1.0)
    metrics_repo.record("default", "metric_b", 2.0)
    points = metrics_repo.query("default", "metric_a", bucket="1 minute")
    assert all(p.count == 1 for p in points)


def test_query_filters_by_time_range(metrics_repo) -> None:
    # Nothing in the future
    future = datetime.now(UTC) + timedelta(hours=2)
    points = metrics_repo.query("default", "metric", bucket="1 minute", from_ts=future)
    assert points == []
