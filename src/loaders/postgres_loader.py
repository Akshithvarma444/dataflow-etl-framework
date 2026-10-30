"""PostgreSQL publishing: stage, archive what changes, then merge.

Every statement runs in the caller's transaction, and this class never commits. If anything
later fails, all of it rolls back, which is what makes a retry safe.
"""

import uuid
from collections import Counter

import pandas as pd
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from src.core.constants import DQ_WARNINGS, LoadStrategy, OPS_SCHEMA, ROW_HASH, SOURCE_FILE, SOURCE_ROW
from src.core.contract import TableContract
from src.database.connection import classify_database_error
from src.loaders.base import BaseLoader, LoadCounts, row_hashes, serialize_frame

AUDIT_INSERT = ["first_run_id", "last_run_id", ROW_HASH, SOURCE_FILE, SOURCE_ROW, DQ_WARNINGS]


class PostgresLoader(BaseLoader):
    def __init__(self, schema: str, dataset: str, run_id: uuid.UUID, stage_method: str = "copy") -> None:
        self.schema = schema
        self.dataset = dataset
        self.run_id = run_id
        self.stage_method = stage_method

    def _target(self, table: TableContract) -> str:
        return f"{self.schema}.{table.name}"

    def _stage(self, table: TableContract) -> str:
        return f"stage_{table.name}"

    def _seen(self, table: TableContract) -> str:
        return f"seen_{table.name}"

    def prepare(self, conn: Connection, table: TableContract) -> None:
        conn.execute(text(
            f"CREATE TEMP TABLE IF NOT EXISTS {self._stage(table)} "
            f"(LIKE {self._target(table)} INCLUDING DEFAULTS) ON COMMIT DROP"
        ))
        if table.load.strategy is LoadStrategy.REPLACE_SOURCE:
            key_columns = ", ".join(table.primary_key)
            conn.execute(text(
                f"CREATE TEMP TABLE IF NOT EXISTS {self._seen(table)} ON COMMIT DROP AS "
                f"SELECT {key_columns} FROM {self._target(table)} WITH NO DATA"
            ))
            conn.execute(text(f"TRUNCATE {self._seen(table)}"))

    def _write_stage(self, conn: Connection, table: TableContract, columns: list[str], rows: list[tuple]) -> None:
        conn.execute(text(f"TRUNCATE {self._stage(table)}"))
        stage = self._stage(table)
        cursor = conn.connection.dbapi_connection.cursor()
        try:
            if self.stage_method == "copy":
                with cursor.copy(f"COPY {stage} ({', '.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
            else:
                # Row-at-a-time alternative, kept only so the benchmark can measure the difference.
                statement = f"INSERT INTO {stage} ({', '.join(columns)}) SELECT {_column_casts(table, columns)}"
                cursor.executemany(statement, rows)
        finally:
            cursor.close()

    def _merge(self, conn: Connection, table: TableContract, columns: list[str]) -> LoadCounts:
        target, stage = self._target(table), self._stage(table)
        join = " AND ".join(f"t.{key} = s.{key}" for key in table.primary_key)
        version = table.load.version_column
        fresh = f"(s.{version} IS NULL OR t.{version} IS NULL OR s.{version} >= t.{version})" if version else "TRUE"

        stale = 0
        if version:
            stale = conn.execute(text(
                f"SELECT count(*) FROM {stage} s JOIN {target} t ON {join} "
                f"WHERE s.{ROW_HASH} <> t.{ROW_HASH} AND NOT {fresh}"
            )).scalar_one()

        key_json = ", ".join(f"'{key}', t.{key}" for key in table.primary_key)
        conn.execute(text(
            f"INSERT INTO {OPS_SCHEMA}.record_history "
            "(dataset_name, target_table, change_type, business_key, previous_record, superseded_by_run_id) "
            f"SELECT CAST(:dataset AS text), CAST(:table AS text), 'update', jsonb_build_object({key_json}), to_jsonb(t), CAST(:run AS uuid) "
            f"FROM {target} t JOIN {stage} s ON {join} "
            f"WHERE s.{ROW_HASH} <> t.{ROW_HASH} AND {fresh}"
        ), {"dataset": self.dataset, "table": table.name, "run": self.run_id})

        keep_on_update = set(table.primary_key) | {"first_run_id", "created_at"}
        assignments = [f"{c} = EXCLUDED.{c}" for c in columns if c not in keep_on_update]
        assignments.append("updated_at = now()")
        guard = f"{table.name}.{ROW_HASH} IS DISTINCT FROM EXCLUDED.{ROW_HASH}"
        if version:
            guard += (f" AND (EXCLUDED.{version} IS NULL OR {table.name}.{version} IS NULL "
                      f"OR EXCLUDED.{version} >= {table.name}.{version})")
        # xmax is 0 only on a row this statement inserted, which separates inserts from updates.
        result = conn.execute(text(
            f"INSERT INTO {target} ({', '.join(columns)}) SELECT {', '.join(columns)} FROM {stage} "
            f"ON CONFLICT ({', '.join(table.primary_key)}) DO UPDATE SET {', '.join(assignments)} "
            f"WHERE {guard} RETURNING (xmax = 0) AS inserted"
        ))
        outcome = Counter(row.inserted for row in result)
        total = conn.execute(text(f"SELECT count(*) FROM {stage}")).scalar_one()
        return LoadCounts(
            inserted=outcome[True],
            updated=outcome[False],
            stale=stale,
            skipped=total - outcome[True] - outcome[False] - stale,
        )

    def load_batch(self, conn: Connection, table: TableContract, frame: pd.DataFrame) -> LoadCounts:
        if frame.empty:
            return LoadCounts()
        serialized = serialize_frame(frame, table.all_columns)
        # A duplicate count is settled at the end of a file, so it must not affect the hash.
        exclude = {table.duplicates.count_column} if table.duplicates.count_column else set()
        hashes = row_hashes(serialized, exclude)
        run = str(self.run_id)
        rows = list(zip(
            *serialized.values(),
            [run] * len(frame),
            [run] * len(frame),
            hashes,
            frame[SOURCE_FILE].tolist(),
            [str(value) for value in frame[SOURCE_ROW]],
            [None if pd.isna(value) else value for value in frame[DQ_WARNINGS]],
        ))
        columns = [*serialized.keys(), *AUDIT_INSERT]
        try:
            self._write_stage(conn, table, columns, rows)
            counts = self._merge(conn, table, columns)
            if table.load.strategy is LoadStrategy.REPLACE_SOURCE:
                keys = ", ".join(table.primary_key)
                conn.execute(text(f"INSERT INTO {self._seen(table)} ({keys}) SELECT {keys} FROM {self._stage(table)}"))
            return counts
        except DBAPIError as exc:
            raise classify_database_error(exc) from exc

    def finish_replace_source(self, conn: Connection, table: TableContract, source_file: str) -> int:
        """Remove rows of this file that its new version no longer contains, archiving them."""
        target, seen = self._target(table), self._seen(table)
        match = " AND ".join(f"s.{key} = t.{key}" for key in table.primary_key)
        gone = f"t.{SOURCE_FILE} = :file AND NOT EXISTS (SELECT 1 FROM {seen} s WHERE {match})"
        key_json = ", ".join(f"'{key}', t.{key}" for key in table.primary_key)
        conn.execute(text(
            f"INSERT INTO {OPS_SCHEMA}.record_history "
            "(dataset_name, target_table, change_type, business_key, previous_record, superseded_by_run_id) "
            f"SELECT CAST(:dataset AS text), CAST(:table AS text), 'delete', jsonb_build_object({key_json}), to_jsonb(t), CAST(:run AS uuid) "
            f"FROM {target} t WHERE {gone}"
        ), {"dataset": self.dataset, "table": table.name, "run": self.run_id, "file": source_file})
        return conn.execute(text(f"DELETE FROM {target} t WHERE {gone}"), {"file": source_file}).rowcount

    def apply_duplicate_counts(self, conn: Connection, table: TableContract, counts: Counter) -> None:
        """Write how many source rows each collapsed row represents, once a file is complete."""
        fingerprint, count = table.duplicates.fingerprint_column, table.duplicates.count_column
        conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS stage_counts (fp text, n int) ON COMMIT DROP"))
        conn.execute(text("TRUNCATE stage_counts"))
        cursor = conn.connection.dbapi_connection.cursor()
        try:
            with cursor.copy("COPY stage_counts (fp, n) FROM STDIN") as copy:
                for value, occurrences in counts.items():
                    copy.write_row((value, str(occurrences)))
        finally:
            cursor.close()
        conn.execute(text(
            f"UPDATE {self._target(table)} t SET {count} = c.n FROM stage_counts c "
            f"WHERE t.{fingerprint} = c.fp AND t.{count} IS DISTINCT FROM c.n"
        ))


def _column_casts(table: TableContract, columns: list[str]) -> str:
    """Placeholders with casts, because executemany sends every value as text here."""
    types = {column.name: column.type for column in table.all_columns}
    casts = {"integer": "integer", "decimal": "numeric", "float": "double precision",
             "timestamp": "timestamp", "date": "date", "text": "text"}
    audit = {"first_run_id": "uuid", "last_run_id": "uuid", ROW_HASH: "text", SOURCE_FILE: "text",
             SOURCE_ROW: "bigint", DQ_WARNINGS: "text"}
    return ", ".join(f"%s::{casts[types[c]] if c in types else audit[c]}" for c in columns)
