"""Benchmarks on synthetic data, in a separate database. Every number here is measured."""

import json
import logging
from pathlib import Path

from src.core.config import Settings
from src.core.contract import load_contract
from src.database.connection import create_db_engine, ensure_database
from src.generators.data_generator import generate_dataset
from src.pipelines.pipeline import Pipeline, RunOptions, RunResult

logger = logging.getLogger(__name__)

COMPARISON_SIZE = 10000  # large enough to show a difference, small enough for slow methods


def _row(label: str, size: int, result: RunResult) -> dict:
    metrics = result.metrics
    return {
        "scenario": label, "requested_size": size, "status": str(result.status),
        "records": metrics.total("input"),
        "total_seconds": round(metrics.total_seconds, 3),
        **{f"{stage}_seconds": round(seconds, 3) for stage, seconds in metrics.stage_seconds.items()},
        "records_per_second": round(metrics.throughput, 1),
        "peak_rss_mb": round(metrics.peak_rss_mb, 1),
        "inserted": metrics.load_total("inserted"),
        "unchanged": metrics.load_total("skipped"),
    }


def run_benchmark(settings: Settings, dataset: str, sizes: list[int]) -> Path:
    """Measure each size, a rerun of the largest, and COPY against row-at-a-time staging."""
    database = f"{settings.database.name}_perf"
    ensure_database(settings.database, database)
    engine = create_db_engine(settings.database, name=database)
    contract = load_contract(settings.paths.datasets_config_dir / f"{dataset}.yaml")
    base = settings.paths.samples_dir if max(sizes) <= 100 else settings.paths.generated_dir

    def source(size: int) -> Path:
        directory = (settings.paths.samples_dir if size <= 100 else base) / dataset / f"rows_{size}" / "clean"
        if not directory.is_dir():
            generate_dataset(settings, dataset, size)
        return directory

    def run(size: int, **options) -> RunResult:
        return Pipeline(settings, contract, engine,
                        RunOptions(source_dir=source(size), write_outputs=False, **options)).run()

    rows = []
    try:
        for size in sizes:
            logger.info("Benchmarking %s at %d", dataset, size)
            rows.append(_row("full_load", size, run(size, full_refresh=True)))
        largest = max(sizes)
        rows.append(_row("rerun_unchanged_files", largest, run(largest)))
        rows.append(_row("backfill_identical_data", largest, run(largest, backfill=True)))
        if COMPARISON_SIZE in sizes:
            for method in ("copy", "executemany"):
                rows.append(_row(f"staging_{method}", COMPARISON_SIZE,
                                 run(COMPARISON_SIZE, full_refresh=True, stage_method=method)))
    finally:
        engine.dispose()

    output = settings.paths.reports_dir / f"performance_{dataset}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"dataset": dataset, "database": "separate _perf database", "results": rows}, indent=2),
                      encoding="utf-8")
    return output
