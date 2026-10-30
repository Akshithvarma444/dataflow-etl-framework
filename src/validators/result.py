"""Collects rule failures for a batch and turns them into per-record results."""

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.core.constants import RuleSeverity


@dataclass(frozen=True)
class Issue:
    rule: str
    severity: RuleSeverity
    mask: np.ndarray
    column: str | None
    message: str


class IssueCollector:
    """Rule failures as boolean masks: fast to record, and one record can have many."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.issues: list[Issue] = []

    def add(self, rule: str, severity: RuleSeverity, mask: pd.Series | np.ndarray,
            column: str | None = None, message: str = "") -> None:
        if isinstance(mask, pd.Series):
            mask = mask.fillna(False).to_numpy(dtype=bool)
        if mask.any():
            self.issues.append(Issue(rule, severity, mask, column, message))

    def error_mask(self) -> np.ndarray:
        mask = np.zeros(self.size, dtype=bool)
        for issue in self.issues:
            if issue.severity is RuleSeverity.ERROR:
                mask |= issue.mask
        return mask

    def failures_by_position(self, severity: RuleSeverity) -> dict[int, list[Issue]]:
        grouped: dict[int, list[Issue]] = {}
        for issue in self.issues:
            if issue.severity is severity:
                for position in np.flatnonzero(issue.mask):
                    grouped.setdefault(int(position), []).append(issue)
        return grouped


@dataclass
class ValidationResult:
    """What validating one batch produced. Every input row is in exactly one bucket."""

    valid: pd.DataFrame
    rejected: list[dict]
    rejected_positions: np.ndarray
    collapsed: int = 0
    duplicates_flagged: int = 0
    missing_key_rows: int = 0
    duplicate_key_rows: int = 0
    rule_failures: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
