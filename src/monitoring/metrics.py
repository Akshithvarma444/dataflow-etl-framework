"""Counters and timers for one run."""

import math
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import psutil

from src.loaders.base import LoadCounts

STAGES = ("extraction", "validation", "transformation", "loading")


@dataclass
class TableMetrics:
    """Counts for one table. ``input == rejected + collapsed + valid`` must always hold."""

    input: int = 0
    valid: int = 0
    rejected: int = 0
    malformed: int = 0
    collapsed: int = 0
    duplicates_flagged: int = 0
    duplicate_keys: int = 0
    missing_keys: int = 0
    reprocessed: int = 0
    failed: int = 0
    files_processed: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    load: LoadCounts = field(default_factory=LoadCounts)
    rule_failures: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    source_aggregates: dict[str, list[float]] = field(default_factory=dict)
    schema_notes: list[dict] = field(default_factory=list)

    def add_source_value(self, name: str, value: float) -> None:
        """Add a batch's subtotal, so memory does not grow with the number of rows."""
        self.source_aggregates.setdefault(name, []).append(value)

    def aggregate_total(self, name: str) -> float:
        return math.fsum(self.source_aggregates.get(name, []))


@dataclass
class RunMetrics:
    tables: dict[str, TableMetrics] = field(default_factory=dict)
    stage_seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    peak_rss_mb: float = 0.0
    total_seconds: float = 0.0

    def table(self, name: str) -> TableMetrics:
        return self.tables.setdefault(name, TableMetrics())

    @contextmanager
    def timed(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + time.perf_counter() - start

    def sample_memory(self) -> None:
        rss = psutil.Process().memory_info().rss / (1024 * 1024)
        self.peak_rss_mb = max(self.peak_rss_mb, rss)

    def total(self, attribute: str) -> int:
        return sum(getattr(table, attribute) for table in self.tables.values())

    def load_total(self, attribute: str) -> int:
        return sum(getattr(table.load, attribute) for table in self.tables.values())

    def mark_rolled_back(self) -> None:
        """After a rollback nothing was published, whatever the counters had reached."""
        for table in self.tables.values():
            table.failed = table.valid
            table.load = LoadCounts()

    @property
    def failure_rate(self) -> float:
        total = self.total("input")
        return self.total("rejected") / total if total else 0.0

    @property
    def throughput(self) -> float:
        return self.total("input") / self.total_seconds if self.total_seconds else 0.0

    def as_rows(self) -> list[dict]:
        """Long-format rows for ``pipeline_metrics``."""
        rows = [
            {"table_name": "_run", "metric_name": "total_seconds", "metric_value": round(self.total_seconds, 3)},
            {"table_name": "_run", "metric_name": "throughput_rows_per_second", "metric_value": round(self.throughput, 1)},
            {"table_name": "_run", "metric_name": "validation_failure_rate", "metric_value": round(self.failure_rate, 6)},
            {"table_name": "_run", "metric_name": "peak_rss_mb", "metric_value": round(self.peak_rss_mb, 1)},
        ]
        rows += [
            {"table_name": "_run", "metric_name": f"{stage}_seconds", "metric_value": round(seconds, 3)}
            for stage, seconds in self.stage_seconds.items()
        ]
        for name, table in self.tables.items():
            values = {
                "input": table.input, "valid": table.valid, "rejected": table.rejected,
                "malformed": table.malformed, "collapsed": table.collapsed,
                "duplicates_flagged": table.duplicates_flagged, "duplicate_keys": table.duplicate_keys,
                "missing_keys": table.missing_keys, "reprocessed": table.reprocessed, "failed": table.failed,
                "inserted": table.load.inserted, "updated": table.load.updated,
                "skipped": table.load.skipped, "stale": table.load.stale, "deleted": table.load.deleted,
            }
            values.update({f"rule:{rule}": count for rule, count in table.rule_failures.items()})
            values.update({f"warning:{rule}": count for rule, count in table.warnings.items()})
            rows += [{"table_name": name, "metric_name": k, "metric_value": v} for k, v in values.items()]
        return rows
