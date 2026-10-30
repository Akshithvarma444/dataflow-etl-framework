"""Duplicate handling across a whole file, including across batch boundaries."""

import hashlib
from collections import Counter

import numpy as np
import pandas as pd

from src.core.constants import DUPLICATE_KEY, RuleSeverity
from src.core.contract import TableContract
from src.validators.result import IssueCollector

SEPARATOR = "\x1f"
NULL_MARK = "\x00"


def fingerprints(raw: pd.DataFrame, columns: list[str]) -> list[str]:
    """Hash source values exactly as supplied, so it identifies true copies of a row."""
    present = [raw[column].tolist() if column in raw.columns else [None] * len(raw) for column in columns]
    return [
        hashlib.sha256(SEPARATOR.join(v if isinstance(v, str) else NULL_MARK for v in row).encode("utf-8")).hexdigest()
        for row in zip(*present)
    ]


class DuplicateTracker:
    """State for one file. Rows that already failed validation never claim a key."""

    def __init__(self, table: TableContract) -> None:
        self.table = table
        self.policy = table.duplicates.policy
        self.seen_keys: set[tuple] = set()
        self.counts: Counter[str] = Counter()

    def check_keys(self, frame: pd.DataFrame, candidates: np.ndarray, issues: IssueCollector) -> int:
        """REJECT policy: quarantine later occurrences of a primary key."""
        key_columns = [frame[column].tolist() for column in self.table.primary_key]
        duplicate = np.zeros(len(frame), dtype=bool)
        for position, key in enumerate(zip(*key_columns)):
            if not candidates[position]:
                continue
            if key in self.seen_keys:
                duplicate[position] = True
            else:
                self.seen_keys.add(key)
        issues.add(DUPLICATE_KEY, RuleSeverity.ERROR, duplicate, None,
                   f"Primary key {list(self.table.primary_key)} already seen in this file.")
        return int(duplicate.sum())

    def occurrences(self, values: list[str]) -> list[int]:
        """Count each fingerprint: 1 is its first sighting in this file."""
        result = []
        for value in values:
            self.counts[value] += 1
            result.append(self.counts[value])
        return result
