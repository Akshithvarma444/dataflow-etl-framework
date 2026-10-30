"""Runs every validation step on a batch and splits it into valid and rejected rows."""

import hashlib
import json
from collections import Counter

import numpy as np
import pandas as pd

from src.core.constants import (
    DQ_WARNINGS,
    DUPLICATE_KEY,
    DuplicatePolicy,
    RuleSeverity,
    SOURCE_FILE,
    SOURCE_ROW,
)
from src.core.contract import TableContract
from src.extractors.base import MalformedRow, RawBatch, SourceFile
from src.validators.business_validator import CustomRule, apply_rules
from src.validators.duplicate_validator import DuplicateTracker, fingerprints
from src.validators.referential_validator import check_references
from src.validators.result import IssueCollector, ValidationResult
from src.validators.schema_validator import coerce_types


def _storable(value: object) -> object:
    """Make a value storable as JSONB, which cannot hold NUL, however it was malformed."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, list):
        return [_storable(item) for item in value]
    if isinstance(value, dict):
        return {key: _storable(item) for key, item in value.items()}
    return None if value is None or pd.isna(value) else value


def rejection_id(dataset: str, table: str, source_file: str, checksum: str, row_number: int) -> str:
    """Deterministic, so replaying a file cannot quarantine the same record twice."""
    return hashlib.sha256(f"{dataset}|{table}|{source_file}|{checksum}|{row_number}".encode("utf-8")).hexdigest()


class RecordValidator:
    """Holds per-file state, so duplicates are found across batch boundaries."""

    def __init__(self, dataset: str, table: TableContract, source: SourceFile,
                 parent_keys: dict[str, set[str]], custom_rule: CustomRule) -> None:
        self.dataset = dataset
        self.table = table
        self.source = source
        self.parent_keys = parent_keys
        self.custom_rule = custom_rule
        self.duplicates = DuplicateTracker(table)

    def _rejected_from_issues(self, raw: pd.DataFrame, typed: pd.DataFrame, issues: IssueCollector) -> list[dict]:
        records = []
        by_position = issues.failures_by_position(RuleSeverity.ERROR)
        raw_records = raw.drop(columns=[SOURCE_ROW]).to_dict("records") if by_position else []
        for position in sorted(by_position):
            failures = by_position[position]
            row_number = int(typed[SOURCE_ROW].iloc[position])
            records.append({
                "rejection_id": rejection_id(self.dataset, self.table.name, self.source.name, self.source.checksum, row_number),
                "target_table": self.table.name,
                "source_file": self.source.name,
                "source_row_number": row_number,
                "original_record": _storable(raw_records[position]),
                "rejection_reason": "; ".join(sorted(issue.rule for issue in failures)),
                "rule_failures": [
                    {"rule": issue.rule, "column": issue.column, "message": issue.message}
                    for issue in failures
                ],
            })
        return records

    def rejected_from_malformed(self, malformed: list[MalformedRow]) -> list[dict]:
        return [{
            "rejection_id": rejection_id(self.dataset, self.table.name, self.source.name, self.source.checksum, row.source_row_number),
            "target_table": self.table.name,
            "source_file": self.source.name,
            "source_row_number": row.source_row_number,
            "original_record": _storable({"_raw": json.loads(json.dumps(row.raw, default=str)), "_line_number": row.line_number}),
            "rejection_reason": row.reason.split(":")[0],
            "rule_failures": [{"rule": row.reason.split(":")[0], "column": None, "message": row.reason}],
        } for row in malformed]

    def validate(self, batch: RawBatch) -> ValidationResult:
        raw = batch.frame
        issues = IssueCollector(len(raw))
        typed = coerce_types(raw, self.table, issues)
        apply_rules(typed, self.table, issues, self.custom_rule)
        check_references(typed, self.table, self.parent_keys, issues)

        duplicate_policy = self.table.duplicates.policy
        duplicate_key_rows = 0
        if duplicate_policy is DuplicatePolicy.REJECT:
            duplicate_key_rows = self.duplicates.check_keys(typed, ~issues.error_mask(), issues)

        missing_key = sum(
            int(issue.mask.sum()) for issue in issues.issues
            if issue.rule.startswith("missing_required:") and issue.column in self.table.primary_key
        )
        errors = issues.error_mask()
        rejected = self._rejected_from_issues(raw, typed, issues) + self.rejected_from_malformed(batch.malformed)

        keep = ~errors
        collapsed = 0
        duplicates_flagged = 0
        extra: dict[str, list] = {}
        spec = self.table.duplicates
        if duplicate_policy is not DuplicatePolicy.REJECT:
            kept_positions = np.flatnonzero(keep)
            prints = fingerprints(raw.iloc[kept_positions], self.table.source_column_names)
            occurrences = self.duplicates.occurrences(prints)
            if duplicate_policy is DuplicatePolicy.COLLAPSE:
                first = np.array([count == 1 for count in occurrences], dtype=bool)
                collapsed = int((~first).sum())
                keep = np.zeros(len(raw), dtype=bool)
                keep[kept_positions[first]] = True
                extra[spec.fingerprint_column] = [p for p, is_first in zip(prints, first) if is_first]
                extra[spec.count_column] = [1] * int(first.sum())
            else:
                duplicates_flagged = sum(count > 1 for count in occurrences)
                extra[spec.fingerprint_column] = prints
                extra[spec.occurrence_column] = occurrences

        valid = typed[keep].reset_index(drop=True)
        for name, values in extra.items():
            valid[name] = values
        valid[SOURCE_FILE] = self.source.name

        warnings = issues.failures_by_position(RuleSeverity.WARNING)
        labels: list[str | None] = [None] * len(raw)
        warning_counts: Counter = Counter()
        for position, failures in warnings.items():
            if keep[position]:
                labels[position] = ";".join(sorted(issue.rule for issue in failures))
                warning_counts.update(issue.rule for issue in failures)
        valid[DQ_WARNINGS] = pd.Series([labels[i] for i in np.flatnonzero(keep)], dtype=object)

        rule_failures = Counter()
        for issue in issues.issues:
            if issue.severity is RuleSeverity.ERROR:
                rule_failures[issue.rule] += int(issue.mask.sum())
        for row in batch.malformed:
            rule_failures[row.reason.split(":")[0]] += 1

        return ValidationResult(
            valid=valid,
            rejected=rejected,
            rejected_positions=np.flatnonzero(errors),
            collapsed=collapsed,
            duplicates_flagged=duplicates_flagged,
            missing_key_rows=missing_key,
            duplicate_key_rows=duplicate_key_rows,
            rule_failures=rule_failures,
            warnings=warning_counts,
        )
