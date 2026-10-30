"""The generic ETL engine. Nothing here knows about a specific dataset."""

import csv
import json
import logging
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from src.core.config import PROJECT_ROOT, Settings
from src.core.constants import DuplicatePolicy, FK_VIOLATION, LoadStrategy, RunMode, RunStatus, SOURCE_FILE, SOURCE_ROW
from src.core.contract import DatasetContract, TableContract
from src.core.exceptions import ConfigurationError, DataFlowError, SourceNotFoundError, TransientDatabaseError
from src.database.connection import classify_database_error, dataset_lock
from src.database.models import RejectedRecord
from src.database.schema import drop_target_schema, ensure_ops_schema, ensure_target_schema
from src.extractors import create_extractor
from src.extractors.base import RawBatch, SourceFile
from src.loaders.postgres_loader import PostgresLoader
from src.monitoring.audit import AuditLog
from src.monitoring.logger import run_logger
from src.monitoring.metrics import RunMetrics, TableMetrics
from src.reporting.reconciliation import reconcile
from src.transformers.base import load_transformer
from src.validators.referential_validator import load_parent_keys
from src.validators.schema_validator import check_columns
from src.validators.validator import RecordValidator

ORIGIN_MANIFEST = "_manifest.json"
PROCESSED_SAMPLE_ROWS = 100


@dataclass(frozen=True)
class RunOptions:
    source_dir: Path | None = None
    input_files: tuple[Path, ...] = ()
    batch_size: int | None = None
    run_id: uuid.UUID | None = None
    full_refresh: bool = False
    backfill: bool = False
    dry_run: bool = False
    stage_method: str = "copy"
    write_outputs: bool = True

    @property
    def mode(self) -> RunMode:
        if self.dry_run:
            return RunMode.DRY_RUN
        if self.full_refresh:
            return RunMode.FULL_REFRESH
        if self.backfill:
            return RunMode.BACKFILL
        return RunMode.INCREMENTAL


@dataclass
class RunResult:
    run_id: uuid.UUID
    dataset: str
    status: RunStatus
    mode: RunMode
    metrics: RunMetrics
    attempts: int
    failure_reason: str | None = None
    reconciliation: dict = field(default_factory=dict)
    report_path: Path | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is not RunStatus.FAILED


class Pipeline:
    """Runs one dataset: extract, validate, transform, load, reconcile, report."""

    def __init__(self, settings: Settings, contract: DatasetContract, engine: Engine,
                 options: RunOptions = RunOptions()) -> None:
        self.settings = settings
        self.contract = contract
        self.engine = engine
        self.options = options
        self.run_id = options.run_id or uuid.uuid4()
        self.batch_size = options.batch_size or settings.pipeline.batch_size
        self.transformer = load_transformer(contract.transformer)
        self.log = run_logger(__name__, self.run_id, contract.dataset)
        self.audit = AuditLog(engine, contract.dataset, self.run_id, contract.content_hash)
        self.metrics = RunMetrics()
        self.reconciliation: dict = {}
        self.rejected_rows: list[dict] = []

    # ---- paths ------------------------------------------------------------------------

    def _source_dir(self) -> Path:
        if self.options.source_dir:
            return self.options.source_dir
        return PROJECT_ROOT / self.contract.default_source_dir

    def _data_origin(self) -> str:
        """Generated data carries a manifest, so synthetic runs are never mistaken for real ones."""
        directory = self.options.input_files[0].parent if self.options.input_files else self._source_dir()
        manifest = directory / ORIGIN_MANIFEST
        if manifest.is_file():
            return json.loads(manifest.read_text(encoding="utf-8")).get("origin", "unknown")
        return "source"

    def _files_for(self, table: TableContract) -> list[Path]:
        pattern = table.source.pattern
        if self.options.input_files:
            return [path for path in self.options.input_files if path.match(pattern)]
        files = sorted(self._source_dir().glob(pattern))
        if not files:
            raise SourceNotFoundError(f"No file matching {pattern} for {table.name} in {self._source_dir()}")
        return files

    def _check_input_files(self) -> None:
        """An --input file matching no table would otherwise be ignored without a word."""
        for path in self.options.input_files:
            if not any(path.match(table.source.pattern) for table in self.contract.tables):
                raise ConfigurationError(f"{path.name} matches no table of {self.contract.dataset}")

    # ---- run lifecycle ----------------------------------------------------------------

    def run(self) -> RunResult:
        """Execute the run. Failures are recorded and returned, not raised."""
        started_at = datetime.now(UTC)
        start = time.perf_counter()
        status = RunStatus.FAILED
        failure: str | None = None
        attempts = 0
        audit_started = False
        self.log.info("Run started", extra={"stage": "start"})
        try:
            ensure_ops_schema(self.engine)
            self.audit.start_run(self.options.mode, self._data_origin(), started_at)
            audit_started = True
            with dataset_lock(self.engine, self.contract.dataset):
                abandoned = self.audit.mark_abandoned()
                if abandoned:
                    self.log.warning("Marked %d earlier unfinished runs as abandoned", abandoned)
                status, attempts = self._run_with_retries()
        except (DataFlowError, SQLAlchemyError) as exc:
            failure = _short_error(exc)
            self.log.error("Run failed: %s", failure, extra={"stage": "failed", "failure_reason": failure})
        except Exception as exc:  # recorded so the run row never stays 'running'
            failure = f"Unexpected {type(exc).__name__}: {exc}"
            self.log.exception("Run failed unexpectedly", extra={"stage": "failed", "failure_reason": failure})

        self.metrics.total_seconds = time.perf_counter() - start
        if status is RunStatus.FAILED:
            self.metrics.mark_rolled_back()
        if audit_started:
            try:
                self.audit.finish_run(status, self.metrics, attempts, failure)
            except SQLAlchemyError as exc:
                self.log.error("Could not record run outcome: %s", _short_error(exc))

        result = RunResult(self.run_id, self.contract.dataset, status, self.options.mode, self.metrics,
                           attempts, failure, self.reconciliation)
        if self.options.write_outputs:
            result.report_path = self._write_report(result, started_at)
            if status is not RunStatus.FAILED and not self.options.dry_run:
                self._write_rejected_csv()
        self.log.info(
            "Run finished: %s", status,
            extra={"stage": "end", "status": str(status), "input_count": self.metrics.total("input"),
                   "valid_count": self.metrics.total("valid"), "rejected_count": self.metrics.total("rejected"),
                   "inserted_count": self.metrics.load_total("inserted"),
                   "updated_count": self.metrics.load_total("updated"),
                   "skipped_count": self.metrics.load_total("skipped"),
                   "duration_seconds": round(self.metrics.total_seconds, 3)},
        )
        return result

    def _run_with_retries(self) -> tuple[RunStatus, int]:
        """Retry transient failures. Safe because a failed attempt published nothing."""
        pipeline = self.settings.pipeline
        attempt = 0
        while True:
            attempt += 1
            self.metrics = RunMetrics()
            self.reconciliation = {}
            self.rejected_rows = []
            try:
                return self._attempt(), attempt
            except TransientDatabaseError as exc:
                if attempt > pipeline.max_retries:
                    raise
                delay = pipeline.retry_backoff_seconds * 2 ** (attempt - 1)
                self.log.warning("Attempt %d failed (%s); retrying in %.1fs", attempt, exc, delay)
                time.sleep(delay)

    def _attempt(self) -> RunStatus:
        try:
            return self._attempt_in_transaction()
        except DBAPIError as exc:
            raise classify_database_error(exc) from exc

    def _attempt_in_transaction(self) -> RunStatus:
        with self.engine.connect() as conn:
            transaction = conn.begin()
            try:
                if self.options.full_refresh:
                    self.log.warning("Full refresh: dropping and rebuilding %s", self.contract.db_schema)
                    drop_target_schema(conn, self.contract)
                    conn.execute(
                        text("DELETE FROM ops.processed_files WHERE dataset_name = :d"),
                        {"d": self.contract.dataset},
                    )
                ensure_target_schema(conn, self.contract)
                self._check_input_files()

                any_processed = False
                for table in self.contract.tables:
                    for path in self._files_for(table):
                        any_processed |= self._process_file(conn, table, path)

                if not any_processed:
                    self.log.info("Every source file is unchanged; nothing to do", extra={"stage": "skip"})
                    self.reconciliation = reconcile(conn, self.contract, self.metrics)
                    transaction.commit()
                    return RunStatus.SKIPPED

                self._retry_quarantined_references(conn)
                self._refresh_derived_tables(conn)
                self.reconciliation = reconcile(conn, self.contract, self.metrics)
                if self.options.dry_run:
                    self.log.info("Dry run: rolling back", extra={"stage": "dry_run"})
                    transaction.rollback()
                else:
                    transaction.commit()
                    if self.options.write_outputs:
                        self._write_processed_samples()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
        return RunStatus.SUCCESS_WITH_REJECTS if self.metrics.total("rejected") else RunStatus.SUCCESS

    # ---- one file ---------------------------------------------------------------------

    def _process_file(self, conn: Connection, table: TableContract, path: Path) -> bool:
        """Publish one file into the open transaction. Returns False if it was skipped."""
        metrics = self.metrics.table(table.name)
        with self.metrics.timed("extraction"):
            extractor = create_extractor(path, table.source)
        source = extractor.source
        extra = {"stage": "extract", "table": table.name, "source_file": source.name}

        force = self.options.full_refresh or self.options.backfill
        if not force and self.audit.file_already_processed(conn, source.name, source.checksum):
            metrics.files_skipped.append(source.name)
            self.log.info("%s unchanged, skipped", source.name, extra=extra)
            return False

        metrics.schema_notes.append(
            {"file": source.name, **check_columns(extractor.columns(), table, source.name)}
        )
        parent_keys = load_parent_keys(conn, self.contract, table)
        validator = RecordValidator(self.contract.dataset, table, source, parent_keys, self.transformer.custom_rule)
        loader = PostgresLoader(self.contract.db_schema, self.contract.dataset, self.run_id, self.options.stage_method)
        loader.prepare(conn, table)

        rows_before = metrics.input
        batches = extractor.read_batches(self.batch_size)
        while True:
            with self.metrics.timed("extraction"):
                batch = next(batches, None)
            if batch is None:
                break
            self._process_batch(conn, table, batch, validator, loader, metrics)
            self.metrics.sample_memory()

        rows = metrics.input - rows_before
        with self.metrics.timed("loading"):
            if table.duplicates.policy is DuplicatePolicy.COLLAPSE:
                loader.apply_duplicate_counts(conn, table, validator.duplicates.counts)
            if table.load.strategy is LoadStrategy.REPLACE_SOURCE and rows > 0:
                # A header-only file is an empty batch, not an instruction to delete everything.
                metrics.load.deleted += loader.finish_replace_source(conn, table, source.name)
            self.audit.mark_file_processed(conn, source.name, source.checksum, rows)
        metrics.files_processed.append(source.name)
        if rows == 0:
            self.log.warning("%s has a header but no rows", source.name, extra=extra)
        self.log.info("%s processed: %d rows", source.name, rows, extra={**extra, "stage": "load", "input_count": rows})
        return True

    def _process_batch(self, conn: Connection, table: TableContract, batch: RawBatch, validator: RecordValidator,
                       loader: PostgresLoader, metrics: TableMetrics) -> None:
        with self.metrics.timed("validation"):
            result = validator.validate(batch)

        metrics.input += len(batch.frame) + len(batch.malformed)
        metrics.malformed += len(batch.malformed)
        metrics.rejected += len(result.rejected)
        metrics.valid += len(result.valid)
        metrics.collapsed += result.collapsed
        metrics.duplicates_flagged += result.duplicates_flagged
        metrics.duplicate_keys += result.duplicate_key_rows
        metrics.missing_keys += result.missing_key_rows
        metrics.rule_failures.update(result.rule_failures)
        metrics.warnings.update(result.warnings)
        self._accumulate_source_totals(table, batch.frame, result.rejected_positions, metrics)

        with self.metrics.timed("transformation"):
            transformed = self.transformer.transform(table, result.valid)
        with self.metrics.timed("loading"):
            metrics.load.add(loader.load_batch(conn, table, transformed))
            self.audit.write_rejections(conn, result.rejected)
        self.rejected_rows.extend(result.rejected)

    def _accumulate_source_totals(self, table: TableContract, raw: pd.DataFrame, rejected_positions, metrics: TableMetrics) -> None:
        """Totals from raw text, for the rows that were not rejected."""
        kept = raw.drop(raw.index[rejected_positions])
        for aggregate in table.aggregates:
            if not aggregate.source_columns:
                metrics.add_source_value(aggregate.name, float(len(kept)))
                continue
            for column in aggregate.source_columns:
                if column in kept.columns:
                    numbers = pd.to_numeric(kept[column], errors="coerce").dropna()
                    metrics.add_source_value(aggregate.name, math.fsum(numbers.astype("float64")))

    # ---- after all files --------------------------------------------------------------

    def _retry_quarantined_references(self, conn: Connection) -> None:
        """Give rows rejected only for a missing parent another chance, now that parents arrived.

        This is how late-arriving data recovers without touching any source file.
        """
        rows = conn.execute(
            text("SELECT rejection_id, target_table, source_file, source_row_number, original_record, rejection_reason "
                 "FROM ops.rejected_records WHERE dataset_name = :d AND resolved_at IS NULL "
                 "ORDER BY target_table, source_file, source_row_number"),
            {"d": self.contract.dataset},
        ).mappings().all()
        fk_only = [
            row for row in rows
            if all(reason.startswith(FK_VIOLATION) for reason in row["rejection_reason"].split("; "))
        ]
        for table in self.contract.tables:
            for source_file in sorted({r["source_file"] for r in fk_only if r["target_table"] == table.name}):
                self._retry_group(conn, table, source_file, [
                    r for r in fk_only if r["target_table"] == table.name and r["source_file"] == source_file
                ])

    def _retry_group(self, conn: Connection, table: TableContract, source_file: str, rows: list) -> None:
        parent_keys = load_parent_keys(conn, self.contract, table)
        source = SourceFile(Path(source_file), source_file, "replay", 0, "utf-8")
        validator = RecordValidator(self.contract.dataset, table, source, parent_keys, self.transformer.custom_rule)
        frame = pd.DataFrame([r["original_record"] for r in rows], dtype=object)
        frame[SOURCE_ROW] = pd.array([r["source_row_number"] for r in rows], dtype="int64")
        result = validator.validate(RawBatch(frame))
        recovered = result.valid[SOURCE_ROW].tolist()
        if not recovered:
            return

        loader = PostgresLoader(self.contract.db_schema, self.contract.dataset, self.run_id, self.options.stage_method)
        loader.prepare(conn, table)
        transformed = self.transformer.transform(table, result.valid)
        metrics = self.metrics.table(table.name)
        metrics.load.add(loader.load_batch(conn, table, transformed))
        metrics.reprocessed += len(recovered)

        by_row = {r["source_row_number"]: r["rejection_id"] for r in rows}
        conn.execute(
            text("UPDATE ops.rejected_records SET resolved_at = now(), resolved_by_run_id = :run "
                 "WHERE rejection_id = ANY(:ids)"),
            {"run": self.run_id, "ids": [by_row[row] for row in recovered]},
        )
        self.log.info("Recovered %d quarantined %s rows", len(recovered), table.name,
                      extra={"stage": "reprocess", "table": table.name})

    def _refresh_derived_tables(self, conn: Connection) -> None:
        with self.metrics.timed("loading"):
            for derived in self.contract.derived_tables:
                for statement in derived.refresh_sql:
                    conn.execute(text(statement))

    # ---- outputs ----------------------------------------------------------------------

    def _output_dir(self, base: Path) -> Path:
        directory = base / self.contract.dataset
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_report(self, result: RunResult, started_at: datetime) -> Path:
        metrics = result.metrics
        report = {
            "run_id": str(result.run_id),
            "dataset": result.dataset,
            "status": str(result.status),
            "mode": str(result.mode),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "attempts": result.attempts,
            "failure_reason": result.failure_reason,
            "totals": {
                "input": metrics.total("input"), "valid": metrics.total("valid"),
                "rejected": metrics.total("rejected"), "inserted": metrics.load_total("inserted"),
                "updated": metrics.load_total("updated"), "skipped": metrics.load_total("skipped"),
                "stale": metrics.load_total("stale"), "deleted": metrics.load_total("deleted"),
                "failed": metrics.total("failed"), "reprocessed": metrics.total("reprocessed"),
                "validation_failure_rate": round(metrics.failure_rate, 6),
            },
            "performance": {
                "total_seconds": round(metrics.total_seconds, 3),
                "throughput_rows_per_second": round(metrics.throughput, 1),
                "peak_rss_mb": round(metrics.peak_rss_mb, 1),
                "stage_seconds": {stage: round(seconds, 3) for stage, seconds in metrics.stage_seconds.items()},
            },
            "reconciliation": result.reconciliation,
            "schema_notes": {name: table.schema_notes for name, table in metrics.tables.items() if table.schema_notes},
        }
        directory = self.settings.paths.reports_dir / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{result.dataset}_{stamp}_{result.run_id}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _write_rejected_csv(self) -> None:
        if not self.rejected_rows:
            return
        path = self._output_dir(self.settings.paths.rejected_dir) / f"rejected_{self.run_id}.csv"
        columns = ["rejection_id", "target_table", "source_file", "source_row_number", "rejection_reason", "original_record"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in self.rejected_rows:
                writer.writerow({**row, "original_record": json.dumps(row["original_record"], ensure_ascii=False)})

    def _write_processed_samples(self) -> None:
        """A small sample of each published table, as a quick look at the output."""
        directory = self._output_dir(self.settings.paths.processed_dir)
        names = [t.name for t in self.contract.tables] + [t.name for t in self.contract.derived_tables]
        with self.engine.connect() as conn:
            for name in names:
                frame = pd.read_sql(
                    text(f"SELECT * FROM {self.contract.db_schema}.{name} ORDER BY 1 LIMIT {PROCESSED_SAMPLE_ROWS}"),
                    conn,
                )
                frame.to_csv(directory / f"{name}_sample.csv", index=False)


def _short_error(exc: Exception) -> str:
    """The cause without SQLAlchemy's statement dump, which can be megabytes of row data."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return str(exc.orig).strip()
    return str(exc).strip()
