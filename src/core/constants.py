"""Vocabulary shared across pipeline stages.

These values are stored in the operational tables and written in dataset YAML,
so renaming one is a data migration, not just a refactor.
"""

from enum import StrEnum

SOURCE_ROW = "source_row_number"
SOURCE_FILE = "source_file"
ROW_HASH = "row_hash"
DQ_WARNINGS = "dq_warnings"
OPS_SCHEMA = "ops"
MALFORMED_ROW = "malformed_row"
FK_VIOLATION = "fk_violation"
DUPLICATE_KEY = "duplicate_key"
AGGREGATE_TOLERANCE = 0.01


class RunStatus(StrEnum):
    """Lifecycle of one row in ``pipeline_runs``."""

    RUNNING = "running"
    SUCCESS = "success"
    SUCCESS_WITH_REJECTS = "success_with_rejects"
    SKIPPED = "skipped"  # every source file was already processed
    FAILED = "failed"
    ABANDONED = "abandoned"  # process died; found still "running" by a later run


class RunMode(StrEnum):
    """Why a run was started, which decides how it treats processed files and targets."""

    INCREMENTAL = "incremental"  # the default: only new or changed files
    BACKFILL = "backfill"  # reprocess every file through the normal upsert
    FULL_REFRESH = "full_refresh"  # drop target tables and rebuild them
    DRY_RUN = "dry_run"  # do everything, then roll back


class RuleSeverity(StrEnum):
    """What a failed validation rule does to the record."""

    ERROR = "error"  # record goes to quarantine
    WARNING = "warning"  # record loads and the anomaly is flagged and counted


class DuplicatePolicy(StrEnum):
    """How duplicates of a table's key or fingerprint are treated."""

    REJECT = "reject"  # first occurrence kept, the rest quarantined
    COLLAPSE = "collapse"  # one row kept with a count of how many it represents
    FLAG = "flag"  # all rows kept and marked, for sources with no reliable key


class LoadStrategy(StrEnum):
    """How a table is published from its source file."""

    UPSERT = "upsert"  # files are incremental; absent rows stay in the target
    REPLACE_SOURCE = "replace_source"  # files are snapshots of their own rows
