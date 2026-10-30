"""Foreign-key checks against keys already in the target tables.

The caller loads parents first inside the run's transaction, so a query sees this run's
new parents and those from earlier runs. A skipped parent file therefore still satisfies
its children, and a parent arriving later can resolve a child rejected earlier.
"""

from sqlalchemy import Connection, text

from src.core.constants import FK_VIOLATION, RuleSeverity
from src.core.contract import DatasetContract, TableContract
from src.validators.result import IssueCollector

import pandas as pd


def load_parent_keys(conn: Connection, contract: DatasetContract, table: TableContract) -> dict[str, set[str]]:
    """Return the existing parent keys for each referencing column of ``table``."""
    keys: dict[str, set[str]] = {}
    for reference in table.references:
        rows = conn.execute(
            text(
                f"SELECT DISTINCT {reference.parent_column} "
                f"FROM {contract.db_schema}.{reference.parent_table}"
            )
        )
        keys[reference.column] = {row[0] for row in rows}
    return keys


def check_references(frame: pd.DataFrame, table: TableContract, parent_keys: dict[str, set[str]],
                     issues: IssueCollector) -> None:
    for reference in table.references:
        values = frame[reference.column]
        missing = values.notna() & ~values.isin(parent_keys[reference.column])
        issues.add(
            f"{FK_VIOLATION}:{reference.column}",
            RuleSeverity(reference.on_missing),
            missing,
            reference.column,
            f"No matching {reference.parent_table}.{reference.parent_column}.",
        )
