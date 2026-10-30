"""Break the code on purpose and confirm the tests notice.

Passing tests only mean something if they would fail on wrong code. Each mutation below is a
plausible real bug. It is applied to the source, the relevant tests run, and the file is
restored whatever happens. Output: reports/mutation_check.json.

Usage:
    python scripts/mutation_check.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = [
    ("Stale guard removed: old versions overwrite new", "src/loaders/postgres_loader.py",
     """fresh = f"(s.{version} IS NULL OR t.{version} IS NULL OR s.{version} >= t.{version})" if version else "TRUE\"""",
     'fresh = "TRUE"', "tests/integration/test_postgres_loader.py tests/edge_cases/test_idempotency.py"),
    ("Skip check looks at any earlier load, not the latest", "src/monitoring/audit.py",
     ".order_by(ProcessedFile.processed_at.desc())", ".order_by(ProcessedFile.processed_at.asc())",
     "tests/edge_cases/test_idempotency.py"),
    ("Contract changes no longer reprocess files", "src/monitoring/audit.py",
     "tuple(latest) == (checksum, self.contract_hash)", "latest[0] == checksum",
     "tests/edge_cases/test_idempotency.py"),
    ("Transaction commits even when a load fails midway", "src/pipelines/pipeline.py",
     """            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise""",
     """            except BaseException:
                if transaction.is_active:
                    transaction.commit()
                raise""", "tests/edge_cases/test_retries.py"),
    ("Empty batch deletes the whole snapshot", "src/pipelines/pipeline.py",
     "if table.load.strategy is LoadStrategy.REPLACE_SOURCE and rows > 0:", "if table.load.strategy is LoadStrategy.REPLACE_SOURCE:",
     "tests/edge_cases/test_empty_input.py"),
    ("Reconciliation ignores aggregate differences", "src/reporting/reconciliation.py",
     "if status == MISMATCH:", "if False:", "tests/edge_cases/test_retries.py"),
    ("Rejected rows also load into the target", "src/validators/validator.py",
     "keep = ~errors", "keep = np.ones(len(raw), dtype=bool)", "tests/integration/test_hotel_pipeline.py"),
    ("Duplicate keys are not tracked across batches", "src/validators/duplicate_validator.py",
     "self.seen_keys.add(key)", "pass", "tests/edge_cases/test_duplicates.py tests/unit/test_validation.py"),
    ("Hard references are not checked", "src/validators/referential_validator.py",
     "missing = values.notna() & ~values.isin(parent_keys[reference.column])", "missing = values.isna() & False",
     "tests/edge_cases/test_malformed_input.py"),
    ("NA treated as null everywhere, losing valid values", "src/validators/schema_validator.py",
     "tokens = column.null_tokens if column.null_tokens is not None else table.source.null_tokens",
     'tokens = ("", "NA", "NULL")', "tests/integration/test_hotel_pipeline.py"),
    ("Guest total drops unknown children incorrectly", "src/transformers/hotel_transformer.py",
     'return frame["adults"] + frame["children"].fillna(0) + frame["babies"]',
     'return frame["adults"] + frame["children"] + frame["babies"]',
     "tests/unit/test_transformation.py"),
    ("Booking value ignores weekend nights", "src/transformers/hotel_transformer.py",
     'frame["total_nights"] = _total_nights(frame)', 'frame["total_nights"] = frame["stays_in_week_nights"]',
     "tests/integration/test_hotel_pipeline.py"),
    ("Late-parent recovery never runs", "src/pipelines/pipeline.py",
     "self._retry_quarantined_references(conn)\n", "pass\n", "tests/edge_cases/test_idempotency.py"),
    ("Row hash ignores content, so changes are skipped", "src/loaders/base.py",
     "columns = [values for name, values in serialized.items() if name not in exclude]",
     "columns = [values for name, values in list(serialized.items())[:1] if name not in exclude]",
     "tests/edge_cases/test_idempotency.py"),
    ("Snapshot deletion guard removed", "src/loaders/postgres_loader.py",
     "if existing and removing / existing > table.load.max_delete_fraction:", "if False:",
     "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Superseded rejections stay open and can overwrite corrections", "src/pipelines/pipeline.py",
     "self.audit.supersede_rejections(conn, table.name, source.name)", "pass",
     "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Recovery overwrites rows that arrived another way", "src/pipelines/pipeline.py",
     "already_loaded = pd.Series([key in loaded for key in candidates], index=result.valid.index, dtype=bool)",
     "already_loaded = pd.Series([False for key in candidates], index=result.valid.index, dtype=bool)",
     "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Partial re-delivery treated as comparable", "src/reporting/reconciliation.py",
     "if attributed_rows != metrics.valid:", "if False:", "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Extra decimals silently rounded", "src/validators/schema_validator.py",
     "valid = finite & (numbers.abs().to_numpy() < NUMERIC_MAX) & exact", "valid = finite & (numbers.abs().to_numpy() < NUMERIC_MAX)",
     "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Warnings left out of the row hash", "src/loaders/postgres_loader.py",
     "hashes = row_hashes({**serialized, DQ_WARNINGS: warnings}, exclude)", "hashes = row_hashes(serialized, exclude)",
     "tests/edge_cases/test_recovery_and_accounting.py"),
    ("Retries also repeat permanent failures", "src/pipelines/pipeline.py",
     "except TransientDatabaseError as exc:", "except DataFlowError as exc:", "tests/edge_cases/test_retries.py"),
]


def main() -> int:
    results = []
    for description, relative, old, new, tests in MUTATIONS:
        path = ROOT / relative
        original = path.read_bytes()
        text = original.decode("utf-8")
        if old not in text:
            results.append({"mutation": description, "file": relative, "outcome": "NOT APPLIED: code has changed"})
            continue
        try:
            path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider", *tests.split()],
                                 cwd=ROOT, capture_output=True, text=True)
            lines = run.stdout.strip().splitlines()
            caught = run.returncode == 1
            failing = next((line.split(" - ")[0].replace("FAILED ", "") for line in lines if line.startswith("FAILED")), None)
            results.append({"mutation": description, "file": relative,
                            "outcome": "caught" if caught else "NOT CAUGHT", "first_failing_test": failing})
        finally:
            path.write_bytes(original)
        print(f"{results[-1]['outcome']:12} {description}")

    caught = sum(row["outcome"] == "caught" for row in results)
    output = ROOT / "reports" / "mutation_check.json"
    output.write_text(json.dumps({"mutations": len(results), "caught": caught, "results": results}, indent=2), encoding="utf-8")
    print(f"{caught} of {len(results)} caught. Wrote {output}")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
