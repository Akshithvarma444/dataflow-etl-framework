"""Operational tables, shared by every dataset, in the ``ops`` schema."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.constants import OPS_SCHEMA


class OpsBase(DeclarativeBase):
    pass


class PipelineRun(OpsBase):
    """One row per execution, including skipped and failed ones. Never overwritten by a rerun."""

    __tablename__ = "pipeline_runs"
    __table_args__ = {"schema": OPS_SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dataset_name: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text)
    run_mode: Mapped[str] = mapped_column(Text)
    data_origin: Mapped[str] = mapped_column(Text)
    contract_hash: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_count: Mapped[int | None] = mapped_column(BigInteger)
    valid_count: Mapped[int | None] = mapped_column(BigInteger)
    rejected_count: Mapped[int | None] = mapped_column(BigInteger)
    inserted_count: Mapped[int | None] = mapped_column(BigInteger)
    updated_count: Mapped[int | None] = mapped_column(BigInteger)
    skipped_count: Mapped[int | None] = mapped_column(BigInteger)
    failed_count: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int | None] = mapped_column(BigInteger)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class PipelineMetric(OpsBase):
    """Long format, so a new metric never needs a schema change."""

    __tablename__ = "pipeline_metrics"
    __table_args__ = {"schema": OPS_SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{OPS_SCHEMA}.pipeline_runs.run_id"), primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_value: Mapped[float] = mapped_column(Numeric)


class ProcessedFile(OpsBase):
    """Written in the load transaction, so it can never claim a load that rolled back."""

    __tablename__ = "processed_files"
    __table_args__ = {"schema": OPS_SCHEMA}

    dataset_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_file: Mapped[str] = mapped_column(Text, primary_key=True)
    checksum: Mapped[str] = mapped_column(Text, primary_key=True)
    contract_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{OPS_SCHEMA}.pipeline_runs.run_id"))
    row_count: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class RejectedRecord(OpsBase):
    """Quarantine. ``rejection_id`` is deterministic, so replays do not add duplicates."""

    __tablename__ = "rejected_records"
    __table_args__ = (
        Index("ix_rejected_dataset_open", "dataset_name", "resolved_at"),
        {"schema": OPS_SCHEMA},
    )

    rejection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{OPS_SCHEMA}.pipeline_runs.run_id"))
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    dataset_name: Mapped[str] = mapped_column(Text)
    target_table: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str] = mapped_column(Text)
    source_row_number: Mapped[int] = mapped_column(BigInteger)
    original_record: Mapped[dict] = mapped_column(JSONB)
    rejection_reason: Mapped[str] = mapped_column(Text)
    rule_failures: Mapped[list] = mapped_column(JSONB)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class RecordHistory(OpsBase):
    """Previous values of rows that a later load changed or removed."""

    __tablename__ = "record_history"
    __table_args__ = (
        Index("ix_history_table", "dataset_name", "target_table"),
        {"schema": OPS_SCHEMA},
    )

    history_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_name: Mapped[str] = mapped_column(Text)
    target_table: Mapped[str] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(Text)
    business_key: Mapped[dict] = mapped_column(JSONB)
    previous_record: Mapped[dict] = mapped_column(JSONB)
    superseded_by_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{OPS_SCHEMA}.pipeline_runs.run_id"))
    superseded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
