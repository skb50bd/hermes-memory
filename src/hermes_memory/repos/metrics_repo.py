"""Metrics repository — hermes_metrics.events (timescaledb).

Public surface (from MetricsTools.cs):
  - record(profile, name, value, *, tags) -> int
  - query(profile, name, *, from_ts, to_ts, bucket) -> list[MetricPoint]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class MetricPoint:
    ts: datetime
    p50: float
    p95: float
    p99: float
    count: int


class MetricsRepo:
    def record(
        self,
        profile: str,
        name: str,
        value: float,
        *,
        tags: dict[str, str] | None = None,
    ) -> int:
        if not profile or not name:
            raise ValueError("profile and name must be non-empty")
        return self._insert_event(profile, name, value, tags or {})

    def query(
        self,
        profile: str,
        name: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        bucket: str = "1 minute",
    ) -> list[MetricPoint]:
        return self._query(
            profile, name, from_ts=from_ts, to_ts=to_ts, bucket=bucket
        )

    def _insert_event(self, profile, name, value, tags) -> int:
        raise NotImplementedError

    def _query(self, profile, name, *, from_ts, to_ts, bucket) -> list[MetricPoint]:
        raise NotImplementedError
