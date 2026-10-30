"""Source-to-target reconciliation, run inside the load transaction before it commits.

A difference raises, which rolls the load back: data that does not reconcile is never
published. Source totals come from raw text before validation, so a bug in transformation or
loading cannot hide by affecting both sides.

A total can only be compared when the target rows attributed to this run's files are exactly
the rows this run accepted. When they are not, for example after a partial re-delivery, the
result is "not_comparable" with the reason. It is not a pass, and no difference is excused.
"""

from sqlalchemy import Connection, text

from src.core.constants import AGGREGATE_TOLERANCE, SOURCE_FILE
from src.core.contract import DatasetContract, TableContract
from src.core.exceptions import ReconciliationError
from src.monitoring.metrics import RunMetrics, TableMetrics

MATCH = "match"
NOT_COMPARABLE = "not_comparable"
MISMATCH = "mismatch"


def _not_comparable_reason(metrics: TableMetrics, attributed_rows: int) -> str | None:
    """Why source and target totals cover different rows, or None if they cover the same ones."""
    reasons = []
    if attributed_rows != metrics.valid:
        reasons.append(f"{attributed_rows} target rows are attributed to these files but {metrics.valid} rows were accepted")
    if metrics.load.stale:
        reasons.append(f"{metrics.load.stale} older rows were not applied")
    if metrics.reprocessed or metrics.recovered_same_run:
        reasons.append(f"{metrics.reprocessed + metrics.recovered_same_run} rows were recovered from quarantine")
    return "; ".join(reasons) or None


def _reconcile_table(conn: Connection, schema: str, table: TableContract, metrics: TableMetrics) -> dict:
    target = f"{schema}.{table.name}"
    target_rows = conn.execute(text(f"SELECT count(*) FROM {target}")).scalar_one()
    accounted = metrics.rejected + metrics.collapsed + metrics.valid
    loaded = metrics.load.inserted + metrics.load.updated + metrics.load.skipped + metrics.load.stale

    report = {
        "files_processed": metrics.files_processed,
        "files_skipped": metrics.files_skipped,
        "source_rows": metrics.input,
        "valid_rows": metrics.valid,
        "rejected_rows": metrics.rejected,
        "malformed_rows": metrics.malformed,
        "collapsed_duplicates": metrics.collapsed,
        "flagged_duplicates": metrics.duplicates_flagged,
        "duplicate_key_rows": metrics.duplicate_keys,
        "missing_key_rows": metrics.missing_keys,
        "recovered_from_quarantine": metrics.reprocessed + metrics.recovered_same_run,
        "inserted": metrics.load.inserted,
        "updated": metrics.load.updated,
        "unchanged": metrics.load.skipped,
        "stale_not_applied": metrics.load.stale,
        "deleted": metrics.load.deleted,
        "target_rows_total": target_rows,
        "rejected_by_rule": dict(sorted(metrics.rule_failures.items())),
        "warnings_by_rule": dict(sorted(metrics.warnings.items())),
        "aggregates": [],
    }
    if accounted != metrics.input:
        raise ReconciliationError(
            f"{table.name}: {metrics.input} source rows but {accounted} accounted for "
            f"(rejected {metrics.rejected}, collapsed {metrics.collapsed}, valid {metrics.valid})."
        )
    if loaded != metrics.valid + metrics.reprocessed:
        raise ReconciliationError(
            f"{table.name}: {metrics.valid + metrics.reprocessed} rows sent to load but {loaded} accounted for."
        )

    if not metrics.files_processed:
        return report
    attributed_rows = conn.execute(
        text(f"SELECT count(*) FROM {target} WHERE {SOURCE_FILE} = ANY(:files)"), {"files": metrics.files_processed}
    ).scalar_one()
    reason = _not_comparable_reason(metrics, attributed_rows)
    for aggregate in table.aggregates:
        target_value = conn.execute(
            text(f"SELECT coalesce({aggregate.target_expression}, 0)::float8 FROM {target} "
                 f"WHERE {SOURCE_FILE} = ANY(:files)"),
            {"files": metrics.files_processed},
        ).scalar_one()
        source_value = metrics.aggregate_total(aggregate.name)
        difference = target_value - source_value
        if reason:
            status = NOT_COMPARABLE
        else:
            status = MATCH if abs(difference) <= AGGREGATE_TOLERANCE else MISMATCH
        report["aggregates"].append({
            "name": aggregate.name,
            "source_valid_total": round(source_value, 2),
            "target_total": round(target_value, 2),
            "difference": round(difference, 2),
            "status": status,
            "reason": reason,
        })
        if status == MISMATCH:
            raise ReconciliationError(
                f"{table.name}.{aggregate.name}: source {source_value:.2f}, target {target_value:.2f}, "
                "over the same rows."
            )
    return report


def reconcile(conn: Connection, contract: DatasetContract, metrics: RunMetrics) -> dict:
    """Return the reconciliation report, or raise if anything does not add up."""
    tables = {
        table.name: _reconcile_table(conn, contract.db_schema, table, metrics.table(table.name))
        for table in contract.tables
    }
    checks = []
    for check in contract.checks:
        violations = conn.execute(text(check.sql)).scalar_one()
        checks.append({"name": check.name, "description": check.description,
                       "violations": int(violations), "status": "pass" if violations == 0 else "fail"})
        if violations:
            raise ReconciliationError(f"Check {check.name} found {violations} violations: {check.description}")
    return {"tables": tables, "checks": checks}
