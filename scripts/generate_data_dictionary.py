"""Generate docs/data_dictionary.md from the contracts, so it cannot drift from the code.

Usage:
    python scripts/generate_data_dictionary.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.contract import ColumnSpec, DatasetContract, TableContract, available_datasets, load_contract  # noqa: E402

CONFIG = ROOT / "config" / "datasets"
SQL_TYPES = {"text": "text", "integer": "integer", "decimal": "numeric(14,2)", "float": "double precision",
             "timestamp": "timestamp", "date": "date"}

AUDIT_COLUMNS = """## Columns on every loaded table

| Column | Type | Null | Meaning |
|---|---|---|---|
| `source_file` | text | no | Source file name, without a path, so it is the same on every machine. |
| `source_row_number` | bigint | no | Record number in that file, from 1, excluding the header. Counts records, not lines, because quoted fields can contain newlines. |
| `first_run_id` | uuid | no | The run that first inserted this row. |
| `last_run_id` | uuid | no | The run that last changed it. An unchanged row keeps its old value. |
| `row_hash` | text | no | SHA-256 of the business columns, used to detect change. |
| `dq_warnings` | text | yes | Semicolon-separated warning rules this row triggered. |
| `created_at` | timestamptz | no | When the row was inserted. |
| `updated_at` | timestamptz | no | When the row was last changed. |
"""

OPS_TABLES = """## Operational tables (`ops` schema)

| Table | Grain | Purpose |
|---|---|---|
| `pipeline_runs` | One per execution | Status, mode, data origin, counts, attempts, and failure reason. A rerun adds a row; it never overwrites one. |
| `pipeline_metrics` | One per run, table, and metric | Timings and counts in long format, including a count for each rule. |
| `processed_files` | One per dataset, file, checksum, and contract hash | What was loaded. Written in the load transaction, so it cannot claim a load that rolled back. |
| `rejected_records` | One per rejected source record | Quarantine: `rejection_id`, `run_id`, `last_seen_run_id`, `dataset_name`, `target_table`, `source_file`, `source_row_number`, `original_record` (JSONB, as received), `rejection_reason`, `rule_failures`, `rejected_at`, `resolved_at`, `resolved_by_run_id`. |
| `record_history` | One per superseded or removed row | Earlier values, as JSONB, with the run that replaced them. |
"""


def column_rows(columns: tuple[ColumnSpec, ...], origin: str) -> list[str]:
    return [
        f"| `{c.name}` | {SQL_TYPES[c.type]} | {'no' if c.required else 'yes'} | {origin} | {c.description} |"
        for c in columns
    ]


def table_section(table: TableContract) -> str:
    lines = [f"### `{table.name}`", "", table.description, "", f"- **Grain:** {table.grain}",
             f"- **Primary key:** {', '.join(f'`{k}`' for k in table.primary_key)}",
             f"- **Source file pattern:** `{table.source.pattern}`",
             f"- **Duplicate policy:** {table.duplicates.policy.value}",
             f"- **Load strategy:** {table.load.strategy.value}"
             + (f", version column `{table.load.version_column}`" if table.load.version_column else "")]
    for reference in table.references:
        kind = "foreign key, enforced" if reference.on_missing == "error" else "soft reference, reported as a warning"
        lines.append(f"- **Reference:** `{reference.column}` to `{reference.parent_table}.{reference.parent_column}` ({kind})")
    lines += ["", "| Column | Type | Null | Origin | Meaning |", "|---|---|---|---|---|",
              *column_rows(table.columns, "source"), *column_rows(table.derived_columns, "derived")]
    if table.rules:
        lines += ["", "| Rule | Severity | Check | Note |", "|---|---|---|---|"]
        for rule in table.rules:
            target = rule.column or (f"{rule.left} {rule.op} {rule.right}" if rule.left else "custom logic")
            lines.append(f"| `{rule.name}` | {rule.severity.value} | {rule.check} on {target} | {rule.message} |")
    return "\n".join(lines)


def dataset_section(contract: DatasetContract) -> str:
    parts = [f"## Dataset `{contract.dataset}` (schema `{contract.db_schema}`)", contract.description]
    parts += [table_section(table) for table in contract.tables]
    for derived in contract.derived_tables:
        lines = [f"### `{derived.name}` (derived)", "", derived.description, "", f"- **Grain:** {derived.grain}",
                 f"- **Primary key:** {', '.join(f'`{k}`' for k in derived.primary_key)}", "",
                 "| Column | Type | Null | Origin | Meaning |", "|---|---|---|---|---|",
                 *column_rows(derived.columns, "derived")]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def main() -> None:
    contracts = [load_contract(CONFIG / f"{name}.yaml") for name in available_datasets(CONFIG)]
    intro = ("# Data dictionary\n\nGenerated from `config/datasets/*.yaml` by `scripts/generate_data_dictionary.py`. "
             "Counts quoted are from the real files, in `reports/source_profile.json`.\n\n"
             "Every source column except where marked is read from the file under its exact source name, "
             "including misspellings such as `product_name_lenght`, so lineage to the source stays exact.")
    output = ROOT / "docs" / "data_dictionary.md"
    output.write_text("\n\n".join([intro, AUDIT_COLUMNS, OPS_TABLES, *map(dataset_section, contracts)]) + "\n", encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
