"""Loader interface and the serialization that makes hashes and writes consistent."""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import Connection

from src.core.contract import ColumnSpec, TableContract

SEPARATOR = "\x1f"
NULL_MARK = "\x00"


@dataclass
class LoadCounts:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0  # already present with identical content
    stale: int = 0  # older than the stored version, so not applied
    deleted: int = 0

    def add(self, other: "LoadCounts") -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.skipped += other.skipped
        self.stale += other.stale
        self.deleted += other.deleted


def serialize_column(series: pd.Series, column_type: str) -> list[str | None]:
    """Render values as PostgreSQL text input. Used for hashing and for COPY.

    This runs for every value of every row. The null mask is computed once per column
    because calling pd.isna per value took 15% of a whole hotel run when profiled.
    """
    missing = series.isna().tolist()
    if column_type == "timestamp":
        values = series.dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()
    elif column_type == "date":
        values = series.dt.strftime("%Y-%m-%d").tolist()
    elif column_type == "integer":
        return [None if gap else str(int(value)) for value, gap in zip(series.array, missing)]
    elif column_type in ("decimal", "float"):
        return [None if gap else repr(value) for value, gap in zip(series.astype("float64").tolist(), missing)]
    else:
        values = series.tolist()
    return [None if gap else str(value) for value, gap in zip(values, missing)]


def serialize_frame(frame: pd.DataFrame, columns: tuple[ColumnSpec, ...]) -> dict[str, list[str | None]]:
    return {column.name: serialize_column(frame[column.name], column.type) for column in columns}


def row_hashes(serialized: dict[str, list[str | None]], exclude: set[str]) -> list[str]:
    """Hash business content only, so reloading identical data is recognized as unchanged."""
    columns = [values for name, values in serialized.items() if name not in exclude]
    return [
        hashlib.sha256(SEPARATOR.join(NULL_MARK if v is None else v for v in row).encode("utf-8")).hexdigest()
        for row in zip(*columns)
    ]


class BaseLoader(ABC):
    @abstractmethod
    def prepare(self, conn: Connection, table: TableContract) -> None:
        """Set up for a file's batches."""

    @abstractmethod
    def load_batch(self, conn: Connection, table: TableContract, frame: pd.DataFrame) -> LoadCounts:
        """Publish one validated, transformed batch inside the caller's transaction."""
