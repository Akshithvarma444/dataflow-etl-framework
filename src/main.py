"""Command-line interface. See README.md for every command."""

import argparse
import logging
import sys
import uuid
from pathlib import Path

from src.core.config import load_settings
from src.core.exceptions import DataFlowError
from src.generators.data_generator import generate_dataset
from src.monitoring.logger import configure_logging
from src.pipelines.pipeline import RunOptions, RunResult
from src.pipelines.runner import ALL, resolve_datasets, run_datasets
from src.reporting.performance import run_benchmark

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="DataFlow ETL framework")
    parser.add_argument("--dataset", required=True, help=f"Dataset name, or {ALL!r}")

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--generate", action="store_true", help="Generate synthetic data instead of running")
    modes.add_argument("--benchmark", action="store_true", help="Measure performance on synthetic data")

    parser.add_argument("--rows", type=int, default=1000, help="Rows (hotel) or orders (olist) to generate")
    parser.add_argument("--sizes", default="1000,10000,100000", help="Comma-separated benchmark sizes")
    parser.add_argument("--seed", type=int, help="Generator seed; defaults to settings.yaml")

    parser.add_argument("--source-dir", type=Path, help="Read source files from this directory")
    parser.add_argument("--input", type=Path, action="append", default=[], dest="input_files",
                        help="Process only this file; it is matched to its table by name. Repeatable")
    parser.add_argument("--batch-size", type=int, help="Rows per batch")
    parser.add_argument("--run-id", type=uuid.UUID, help="Use this run ID instead of generating one")

    refresh = parser.add_mutually_exclusive_group()
    refresh.add_argument("--full-refresh", action="store_true", help="Drop target tables and rebuild them")
    refresh.add_argument("--backfill", action="store_true", help="Reprocess all files through the normal upsert")
    parser.add_argument("--dry-run", action="store_true", help="Do everything, then roll back")
    return parser


def _print_result(result: RunResult) -> None:
    metrics = result.metrics
    print(
        f"\n{result.dataset}: {result.status}  run_id={result.run_id}\n"
        f"  input={metrics.total('input')} valid={metrics.total('valid')} rejected={metrics.total('rejected')} "
        f"inserted={metrics.load_total('inserted')} updated={metrics.load_total('updated')} "
        f"unchanged={metrics.load_total('skipped')} stale={metrics.load_total('stale')} "
        f"deleted={metrics.load_total('deleted')} failed={metrics.total('failed')}\n"
        f"  {metrics.total_seconds:.2f}s, {metrics.throughput:,.0f} rows/s, peak {metrics.peak_rss_mb:.0f} MB"
    )
    if result.failure_reason:
        print(f"  failure: {result.failure_reason}")
    if result.report_path:
        print(f"  report: {result.report_path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        configure_logging(settings.logging.level, settings.paths.logs_dir)
        names = resolve_datasets(settings, args.dataset)

        if args.generate:
            for name in names:
                print(generate_dataset(settings, name, args.rows, args.seed))
            return 0
        if args.benchmark:
            sizes = [int(size) for size in args.sizes.split(",")]
            for name in names:
                print(f"Wrote {run_benchmark(settings, name, sizes)}")
            return 0

        options = RunOptions(
            source_dir=args.source_dir, input_files=tuple(args.input_files), batch_size=args.batch_size,
            run_id=args.run_id, full_refresh=args.full_refresh, backfill=args.backfill, dry_run=args.dry_run,
        )
        results = run_datasets(settings, names, options)
        for result in results:
            _print_result(result)
        return 0 if all(result.succeeded for result in results) else 1
    except (DataFlowError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
