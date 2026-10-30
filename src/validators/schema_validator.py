"""File-level column checks, and record-level type and required-field checks.

Values arrive as text and are coerced here, so one bad value rejects one row with the
original value preserved. Coercion is vectorized because it runs on every row.
"""

import logging

import numpy as np
import pandas as pd

from src.core.constants import RuleSeverity, SOURCE_ROW
from src.core.contract import ColumnSpec, TableContract
from src.core.exceptions import SchemaDriftError
from src.validators.result import IssueCollector

logger = logging.getLogger(__name__)

INT_MAX = 2_147_483_647
NUMERIC_MAX = 1e12  # the limit of numeric(14, 2)
DECIMAL_PLACES = 2
DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_DATE_FORMAT = "%Y-%m-%d"


def check_columns(found: list[str], table: TableContract, file_name: str) -> dict[str, list[str]]:
    """Compare a file's header with the contract, failing on drift that makes rows unusable.

    A missing required column stops the file: every row would fail, which is a broken
    delivery and not a data-quality problem. A missing optional column reads as null. An
    unexpected column is ignored, or fails if the contract says so.
    """
    expected = table.source_column_names
    missing = [name for name in expected if name not in found]
    unexpected = [name for name in found if name not in expected]

    missing_required = [c.name for c in table.columns if c.required and c.name in missing]
    if missing_required:
        raise SchemaDriftError(f"{file_name} is missing required columns: {missing_required}")
    if unexpected and table.source.on_unexpected_column == "fail":
        raise SchemaDriftError(f"{file_name} has unexpected columns: {unexpected}")

    if missing:
        logger.warning("%s is missing optional columns, read as null: %s", file_name, missing)
    if unexpected:
        logger.warning("%s has unexpected columns, ignored: %s", file_name, unexpected)
    return {"missing_optional": missing, "unexpected": unexpected}


def _coerce_number(text: pd.Series, column: ColumnSpec, issues: IssueCollector) -> pd.Series:
    numbers = pd.to_numeric(text.str.strip() if len(text) else text, errors="coerce").astype("float64")
    finite = np.isfinite(numbers.to_numpy())
    if column.type == "integer":
        valid = finite & (numbers.abs().to_numpy() <= INT_MAX)
        valid &= np.where(finite, np.floor(numbers.fillna(0).to_numpy()) == numbers.fillna(0).to_numpy(), False)
    elif column.type == "decimal":
        # numeric(14, 2) would round extra decimals without any error, so they are rejected.
        exact = np.isclose(numbers.to_numpy(), np.round(numbers.to_numpy(), DECIMAL_PLACES), rtol=0, atol=1e-9)
        valid = finite & (numbers.abs().to_numpy() < NUMERIC_MAX) & exact
    else:
        valid = finite

    present = text.notna().to_numpy()
    issues.add(f"invalid_type:{column.name}", RuleSeverity.ERROR, present & ~valid, column.name,
               f"Not a valid {column.type}.")
    numbers = numbers.where(valid)
    return numbers.astype("Int64") if column.type == "integer" else numbers


def _coerce_datetime(text: pd.Series, column: ColumnSpec, issues: IssueCollector) -> pd.Series:
    default = DEFAULT_DATE_FORMAT if column.type == "date" else DEFAULT_TIMESTAMP_FORMAT
    parsed = pd.to_datetime(text, format=column.format or default, errors="coerce")
    issues.add(f"invalid_type:{column.name}", RuleSeverity.ERROR,
               text.notna().to_numpy() & parsed.isna().to_numpy(), column.name,
               f"Not a valid {column.type}.")
    return parsed


def coerce_types(raw: pd.DataFrame, table: TableContract, issues: IssueCollector) -> pd.DataFrame:
    """Return a typed frame aligned with ``raw``; unusable values become null and an issue."""
    typed: dict[str, pd.Series] = {SOURCE_ROW: raw[SOURCE_ROW].reset_index(drop=True)}
    for column in table.columns:
        if column.name in raw.columns:
            text = raw[column.name].reset_index(drop=True).astype(object)
        else:
            text = pd.Series([None] * len(raw), dtype=object)
        tokens = column.null_tokens if column.null_tokens is not None else table.source.null_tokens
        text = text.where(~text.isin(tokens) & text.notna(), None)

        if column.required:
            issues.add(f"missing_required:{column.name}", RuleSeverity.ERROR, text.isna().to_numpy(),
                       column.name, "Required value is missing.")

        if column.type in ("integer", "decimal", "float"):
            typed[column.name] = _coerce_number(text, column, issues)
        elif column.type in ("timestamp", "date"):
            typed[column.name] = _coerce_datetime(text, column, issues)
        else:
            has_nul = text.map(lambda value: isinstance(value, str) and "\x00" in value).to_numpy(dtype=bool)
            issues.add(f"invalid_type:{column.name}", RuleSeverity.ERROR, has_nul, column.name,
                       "Contains a NUL character, which PostgreSQL text cannot store.")
            typed[column.name] = text.where(~has_nul, None)
    return pd.DataFrame(typed)
