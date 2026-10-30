"""Run history and file tracking.

Run records use their own short transactions, separate from the load, so they survive
when the load rolls back. Only ``mark_file_processed`` and ``write_rejections`` take the
load's connection, because they must commit or roll back with the data they describe.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.constants import RunStatus
from src.database.models import PipelineMetric, PipelineRun, ProcessedFile, RejectedRecord
from src.monitoring.metrics import RunMetrics


class AuditLog:
    def __init__(self, engine: Engine, dataset: str, run_id: uuid.UUID, contract_hash: str) -> None:
        self.engine = engine
        self.dataset = dataset
        self.run_id = run_id
        self.contract_hash = contract_hash

    def start_run(self, mode: str, origin: str, started_at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(PipelineRun).values(
                run_id=self.run_id, dataset_name=self.dataset, status=RunStatus.RUNNING.value, run_mode=str(mode),
                data_origin=origin, contract_hash=self.contract_hash, started_at=started_at,
            ))

    def mark_abandoned(self) -> int:
        """Called while holding the dataset lock, so any other 'running' row must be dead."""
        with self.engine.begin() as conn:
            return conn.execute(
                update(PipelineRun)
                .where(PipelineRun.dataset_name == self.dataset, PipelineRun.status == RunStatus.RUNNING.value,
                       PipelineRun.run_id != self.run_id)
                .values(status=RunStatus.ABANDONED.value, finished_at=datetime.now(UTC),
                        failure_reason=f"Process ended without finishing; found by run {self.run_id}.")
            ).rowcount

    def finish_run(self, status: RunStatus, metrics: RunMetrics, attempts: int, failure: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(PipelineRun).where(PipelineRun.run_id == self.run_id).values(
                    status=str(status), finished_at=datetime.now(UTC),
                    input_count=metrics.total("input"), valid_count=metrics.total("valid"),
                    rejected_count=metrics.total("rejected"), inserted_count=metrics.load_total("inserted"),
                    updated_count=metrics.load_total("updated"), skipped_count=metrics.load_total("skipped"),
                    failed_count=metrics.total("failed"), attempts=attempts, failure_reason=failure,
                )
            )
            rows = metrics.as_rows()
            if rows:
                conn.execute(insert(PipelineMetric), [{"run_id": self.run_id, **row} for row in rows])

    def file_already_processed(self, conn: Connection, source_file: str, checksum: str) -> bool:
        """True only if the latest load of this file had this content under this contract.

        Checking the latest, not any earlier entry, means restoring a file to a previous
        version is treated as the change it is.
        """
        latest = conn.execute(
            select(ProcessedFile.checksum, ProcessedFile.contract_hash)
            .where(ProcessedFile.dataset_name == self.dataset, ProcessedFile.source_file == source_file)
            .order_by(ProcessedFile.processed_at.desc())
            .limit(1)
        ).first()
        return latest is not None and tuple(latest) == (checksum, self.contract_hash)

    def mark_file_processed(self, conn: Connection, source_file: str, checksum: str, rows: int) -> None:
        statement = pg_insert(ProcessedFile).values(
            dataset_name=self.dataset, source_file=source_file, checksum=checksum,
            contract_hash=self.contract_hash, run_id=self.run_id, row_count=rows,
        )
        conn.execute(statement.on_conflict_do_update(
            index_elements=["dataset_name", "source_file", "checksum", "contract_hash"],
            set_={"run_id": self.run_id, "row_count": rows,
                  "processed_at": datetime.now(UTC)},
        ))

    def write_rejections(self, conn: Connection, records: list[dict]) -> None:
        if not records:
            return
        statement = pg_insert(RejectedRecord)
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=["rejection_id"],
                set_={"last_seen_run_id": statement.excluded.last_seen_run_id,
                      "rejection_reason": statement.excluded.rejection_reason,
                      "rule_failures": statement.excluded.rule_failures,
                      # Rejected again, so it is open again, for example after a full refresh.
                      "resolved_at": None, "resolved_by_run_id": None},
            ),
            [{**record, "run_id": self.run_id, "last_seen_run_id": self.run_id, "dataset_name": self.dataset}
             for record in records],
        )
