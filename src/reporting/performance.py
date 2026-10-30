"""Benchmarks on synthetic data, in a separate database. Every number here is measured.

Each scenario runs several times and reports medians, because a single run on a shared
machine varied by more than the effects being measured. Alternatives being compared are
interleaved, so slow drift in the machine affects them equally.
"""

import json
import logging
import statistics
import time
from pathlib import Path

import pandas as pd

from src.core.config import Settings
from src.core.contract import DatasetContract, load_contract
from src.database.connection import create_db_engine, ensure_database
from src.extractors import create_extractor
from src.generators.data_generator import generate_dataset
from src.loaders.base import serialize_column
from src.pipelines.pipeline import Pipeline, RunOptions, RunResult
from src.transformers.base import load_transformer
from src.validators.validator import RecordValidator

logger = logging.getLogger(__name__)

TRIALS = 3
SERIALIZATION_TRIALS = 5
COMPARISON_SIZE = 10000  # large enough to show a difference, small enough for slow methods


def _summarize(label: str, size: int, results: list[RunResult]) -> dict:
    """Median of each measure across the trials, with the range of the total."""
    def median(value) -> float:
        return round(statistics.median(value(result.metrics) for result in results), 3)

    stages = results[0].metrics.stage_seconds
    totals = [result.metrics.total_seconds for result in results]
    return {
        "scenario": label,
        "requested_size": size,
        "trials": len(results),
        "statuses": sorted({str(result.status) for result in results}),
        "records": results[0].metrics.total("input"),
        "total_seconds_median": median(lambda m: m.total_seconds),
        "total_seconds_min": round(min(totals), 3),
        "total_seconds_max": round(max(totals), 3),
        **{f"{stage}_seconds_median": median(lambda m, s=stage: m.stage_seconds[s]) for stage in stages},
        "records_per_second_median": median(lambda m: m.throughput),
        "peak_rss_mb_median": median(lambda m: m.peak_rss_mb),
        "inserted": results[0].metrics.load_total("inserted"),
        "unchanged": results[0].metrics.load_total("skipped"),
    }


def _serialize_per_value(series: pd.Series, column_type: str) -> list[str | None]:
    """The original implementation, kept only as the baseline for the comparison below."""
    if column_type == "timestamp":
        rendered = series.dt.strftime("%Y-%m-%dT%H:%M:%S").astype(object)
    elif column_type == "date":
        rendered = series.dt.strftime("%Y-%m-%d").astype(object)
    elif column_type == "integer":
        return [None if pd.isna(value) else str(int(value)) for value in series]
    elif column_type in ("decimal", "float"):
        return [None if pd.isna(value) else repr(float(value)) for value in series]
    else:
        rendered = series
    return [None if pd.isna(value) else str(value) for value in rendered]


def _compare_serialization(contract: DatasetContract, source_dir: Path, largest: int) -> list[dict]:
    """Time both serializers on real validated frames, one table at a time."""
    transformer = load_transformer(contract.transformer)
    comparisons = []
    for table in contract.tables:
        if table.references:
            continue  # checking references needs the database, and is not what is measured here
        files = sorted(source_dir.glob(table.source.pattern))
        extractor = create_extractor(files[0], table.source)
        batch = next(extractor.read_batches(largest))
        valid = RecordValidator(contract.dataset, table, extractor.source, {}, transformer.custom_rule).validate(batch).valid
        frame = transformer.transform(table, valid)

        timings: dict[str, list[float]] = {"per_value": [], "column_mask": []}
        for _ in range(SERIALIZATION_TRIALS):
            for name, function in (("per_value", _serialize_per_value), ("column_mask", serialize_column)):
                start = time.perf_counter()
                for column in table.all_columns:
                    function(frame[column.name], column.type)
                timings[name].append(time.perf_counter() - start)
        before, after = statistics.median(timings["per_value"]), statistics.median(timings["column_mask"])
        comparisons.append({
            "table": table.name, "rows": len(frame), "columns": len(table.all_columns), "trials": SERIALIZATION_TRIALS,
            "per_value_seconds_median": round(before, 3), "column_mask_seconds_median": round(after, 3),
            "speedup": round(before / after, 2),
        })
    return comparisons


def run_benchmark(settings: Settings, dataset: str, sizes: list[int]) -> Path:
    """Measure each size, reruns of the largest, and COPY against row-at-a-time staging."""
    database = f"{settings.database.name}_perf"
    ensure_database(settings.database, database)
    engine = create_db_engine(settings.database, name=database)
    contract = load_contract(settings.paths.datasets_config_dir / f"{dataset}.yaml")

    def source(size: int) -> Path:
        base = settings.paths.samples_dir if size <= 100 else settings.paths.generated_dir
        directory = base / dataset / f"rows_{size}" / "clean"
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
            rows.append(_summarize("full_load", size, [run(size, full_refresh=True) for _ in range(TRIALS)]))
        largest = max(sizes)
        run(largest, full_refresh=True)
        rows.append(_summarize("rerun_unchanged_files", largest, [run(largest) for _ in range(TRIALS)]))
        rows.append(_summarize("backfill_identical_data", largest, [run(largest, backfill=True) for _ in range(TRIALS)]))
        if COMPARISON_SIZE in sizes:
            by_method: dict[str, list[RunResult]] = {"copy": [], "executemany": []}
            for _ in range(TRIALS):
                for method, results in by_method.items():
                    results.append(run(COMPARISON_SIZE, full_refresh=True, stage_method=method))
            rows += [_summarize(f"staging_{method}", COMPARISON_SIZE, results) for method, results in by_method.items()]
        serialization = _compare_serialization(contract, source(largest), largest)
    finally:
        engine.dispose()

    output = settings.paths.reports_dir / f"performance_{dataset}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"dataset": dataset, "database": "separate _perf database", "results": rows,
                                  "serialization_comparison": serialization}, indent=2), encoding="utf-8")
    return output
