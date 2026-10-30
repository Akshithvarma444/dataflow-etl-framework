"""The loader against real PostgreSQL: counts, history, ordering, and rollback."""

import uuid
from collections.abc import Callable

import pandas as pd
import pytest
from sqlalchemy import Engine, text

from src.core.contract import ColumnSpec, DatasetContract, LoadSpec, SourceSpec, TableContract
from src.database.schema import ensure_ops_schema, ensure_target_schema
from src.database.models import OpsBase
from src.loaders.base import LoadCounts
from src.loaders.postgres_loader import PostgresLoader
from tests.conftest import rows_of, scalar

TABLE = TableContract(
    name="things",
    source=SourceSpec(pattern="*.csv"),
    columns=(
        ColumnSpec(name="id", type="text", required=True),
        ColumnSpec(name="value", type="decimal"),
        ColumnSpec(name="version_at", type="timestamp"),
    ),
    primary_key=("id",),
    load=LoadSpec(version_column="version_at"),
)
CONTRACT = DatasetContract(
    dataset="things", db_schema="loader_test", default_source_dir="x",
    transformer="src.transformers.base:BaseTransformer", tables=(TABLE,),
)


@pytest.fixture
def loader_db(db: Engine) -> Engine:
    ensure_ops_schema(db)
    with db.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS loader_test CASCADE"))
        ensure_target_schema(conn, CONTRACT)
    return db


def frame(*rows: tuple) -> pd.DataFrame:
    result = pd.DataFrame(rows, columns=["id", "value", "version_at"])
    result["version_at"] = pd.to_datetime(result["version_at"])
    result["value"] = result["value"].astype("float64")
    result["source_file"] = "f.csv"
    result["source_row_number"] = range(1, len(rows) + 1)
    result["dq_warnings"] = None
    return result


@pytest.fixture
def load(loader_db: Engine) -> Callable[..., LoadCounts]:
    def run(data: pd.DataFrame, stage_method: str = "copy") -> LoadCounts:
        run_id = uuid.uuid4()
        with loader_db.begin() as conn:
            conn.execute(text("INSERT INTO ops.pipeline_runs (run_id, dataset_name, status, run_mode, data_origin, contract_hash, started_at) "
                              "VALUES (:r, 'things', 'running', 'test', 'test', 'x', now())"), {"r": run_id})
            loader = PostgresLoader("loader_test", "things", run_id, stage_method)
            loader.prepare(conn, TABLE)
            return loader.load_batch(conn, TABLE, data)
    return run


def test_new_rows_are_inserted(load, loader_db: Engine) -> None:
    counts = load(frame(("a", 1.5, "2020-01-01"), ("b", None, "2020-01-01")))

    assert (counts.inserted, counts.updated, counts.skipped) == (2, 0, 0)
    assert scalar(loader_db, "SELECT value FROM loader_test.things WHERE id = 'a'") == pytest.approx(1.5)
    assert scalar(loader_db, "SELECT value FROM loader_test.things WHERE id = 'b'") is None


def test_identical_rows_are_skipped_and_keep_their_run_id(load, loader_db: Engine) -> None:
    load(frame(("a", 1.5, "2020-01-01")))
    before = scalar(loader_db, "SELECT last_run_id FROM loader_test.things")

    counts = load(frame(("a", 1.5, "2020-01-01")))

    assert (counts.inserted, counts.updated, counts.skipped) == (0, 0, 1)
    assert scalar(loader_db, "SELECT last_run_id FROM loader_test.things") == before
    assert scalar(loader_db, "SELECT count(*) FROM loader_test.things") == 1


def test_changed_row_updates_keeps_first_run_and_archives_the_old_value(load, loader_db: Engine) -> None:
    load(frame(("a", 1.5, "2020-01-01")))
    first = scalar(loader_db, "SELECT first_run_id FROM loader_test.things")

    counts = load(frame(("a", 2.5, "2020-01-02")))

    assert (counts.inserted, counts.updated) == (0, 1)
    row = rows_of(loader_db, "SELECT * FROM loader_test.things")[0]
    assert float(row["value"]) == 2.5
    assert row["first_run_id"] == first
    assert row["last_run_id"] != first
    history = rows_of(loader_db, "SELECT * FROM ops.record_history")
    assert len(history) == 1
    assert history[0]["business_key"] == {"id": "a"}
    assert float(history[0]["previous_record"]["value"]) == 1.5
    assert history[0]["change_type"] == "update"


def test_older_version_is_counted_stale_and_not_applied(load, loader_db: Engine) -> None:
    load(frame(("a", 2.0, "2020-06-01")))

    counts = load(frame(("a", 1.0, "2020-01-01")))

    assert (counts.stale, counts.updated, counts.skipped) == (1, 0, 0)
    assert float(scalar(loader_db, "SELECT value FROM loader_test.things")) == 2.0
    assert scalar(loader_db, "SELECT count(*) FROM ops.record_history") == 0


def test_row_with_no_version_is_not_treated_as_stale(load, loader_db: Engine) -> None:
    load(frame(("a", 2.0, "2020-06-01")))

    counts = load(frame(("a", 3.0, None)))

    assert counts.updated == 1


def test_counts_cover_a_mixed_batch(load) -> None:
    load(frame(("same", 1.0, "2020-06-01"), ("change", 1.0, "2020-06-01"), ("old", 1.0, "2020-06-01")))

    counts = load(frame(("same", 1.0, "2020-06-01"), ("change", 2.0, "2020-06-02"),
                        ("old", 2.0, "2019-01-01"), ("new", 1.0, "2020-06-01")))

    assert (counts.inserted, counts.updated, counts.skipped, counts.stale) == (1, 1, 1, 1)


def test_copy_and_executemany_staging_produce_the_same_result(load, loader_db: Engine) -> None:
    load(frame(("a", 1.5, "2020-01-01"), ("b", None, None)))
    copied = rows_of(loader_db, "SELECT id, value, version_at, row_hash FROM loader_test.things ORDER BY id")
    with loader_db.begin() as conn:
        conn.execute(text("TRUNCATE loader_test.things"))

    counts = load(frame(("a", 1.5, "2020-01-01"), ("b", None, None)), stage_method="executemany")

    assert counts.inserted == 2
    assert rows_of(loader_db, "SELECT id, value, version_at, row_hash FROM loader_test.things ORDER BY id") == copied


def test_nothing_persists_when_the_transaction_rolls_back(loader_db: Engine) -> None:
    run_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        with loader_db.begin() as conn:
            loader = PostgresLoader("loader_test", "things", run_id)
            loader.prepare(conn, TABLE)
            loader.load_batch(conn, TABLE, frame(("a", 1.0, "2020-01-01")))
            raise RuntimeError("later step failed")

    assert scalar(loader_db, "SELECT count(*) FROM loader_test.things") == 0


def test_empty_batch_is_a_no_op(load) -> None:
    empty = frame()

    assert load(empty) == LoadCounts()


def test_target_table_drift_is_refused(loader_db: Engine) -> None:
    from src.core.exceptions import SchemaDriftError

    with loader_db.begin() as conn:
        conn.execute(text("ALTER TABLE loader_test.things ADD COLUMN surprise text"))

    with pytest.raises(SchemaDriftError, match="full-refresh"):
        with loader_db.begin() as conn:
            ensure_target_schema(conn, CONTRACT)


def test_ops_tables_are_in_their_own_schema() -> None:
    assert {table.schema for table in OpsBase.metadata.tables.values()} == {"ops"}
