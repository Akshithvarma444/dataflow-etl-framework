"""Vocabulary shared across pipeline stages.

These values are stored in the operational tables and written in dataset YAML,
so renaming one is a data migration, not just a refactor.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """Lifecycle of one row in ``pipeline_runs``."""

    RUNNING = "running"
    SUCCESS = "success"
    SUCCESS_WITH_REJECTS = "success_with_rejects"
    SKIPPED = "skipped"  # every source file was already processed
    FAILED = "failed"
    ABANDONED = "abandoned"  # process died; found still "running" by a later run


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
