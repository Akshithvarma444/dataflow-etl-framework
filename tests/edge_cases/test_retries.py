"""Failure injection: partial failures roll back completely, and retries are safe."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from src.core.config import Settings
from src.core.constants import RunStatus
from src.core.contract import DatasetContract
from src.core.exceptions import LoadError, TransientDatabaseError
from src.database.connection import create_db_engine, dataset_lock
from src.loaders.postgres_loader import PostgresLoader
from src.pipelines.pipeline import Pipeline, RunOptions
from src.transformers.hotel_transformer import HotelTransformer
from tests.conftest import rows_of, scalar
from tests.helpers import hotel_row, olist_row, write_hotel, write_olist, write_rows


def fail_on_table(monkeypatch: pytest.MonkeyPatch, table_name: str, error: Exception, times: int = 10**6) -> list:
    """Make the loader raise for one table, as a real failure midway through a run would."""
    original = PostgresLoader.load_batch
    calls: list[str] = []

    def load_batch(self, conn, table, frame):
        if table.name == table_name and len(calls) < times:
            calls.append(table.name)
            raise error
        return original(self, conn, table, frame)

    monkeypatch.setattr(PostgresLoader, "load_batch", load_batch)
    return calls


def test_failure_in_a_late_table_rolls_back_every_earlier_table(run_olist, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_olist(tmp_path)
    fail_on_table(monkeypatch, "reviews", LoadError("disk full"))

    result = run_olist(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "disk full" in result.failure_reason
    # The tables were created in the same transaction, so even they rolled back.
    assert scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'olist'") == 0
    assert result.metrics.load_total("inserted") == 0
    assert result.metrics.total("failed") > 0


def test_a_failed_load_never_marks_its_files_as_processed(run_olist, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_olist(tmp_path)
    fail_on_table(monkeypatch, "reviews", LoadError("disk full"))

    run_olist(tmp_path)

    assert scalar(db, "SELECT count(*) FROM ops.processed_files") == 0


def test_failed_run_keeps_its_failure_details_in_history(run_olist, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_olist(tmp_path)
    fail_on_table(monkeypatch, "reviews", LoadError("disk full"))

    result = run_olist(tmp_path)

    row = rows_of(db, "SELECT * FROM ops.pipeline_runs WHERE run_id = :r", r=result.run_id)[0]
    assert row["status"] == "failed"
    assert "disk full" in row["failure_reason"]
    assert row["finished_at"] is not None
    assert row["inserted_count"] == 0


def test_rerun_after_failure_completes_with_no_duplicates(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        fail_on_table(patch, "reviews", LoadError("disk full"))
        assert run_olist(tmp_path).status is RunStatus.FAILED

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.table("reviews").load.inserted == 1
    assert scalar(db, "SELECT count(*) FROM olist.orders") == 1
    assert scalar(db, "SELECT count(*) FROM ops.processed_files") == 9
    assert scalar(db, "SELECT count(*) FROM ops.pipeline_runs") == 2


def test_rollback_of_a_later_run_leaves_earlier_data_exactly_as_it_was(run_olist, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)
    before = rows_of(db, "SELECT * FROM olist.customers")

    write_rows(tmp_path / "olist_customers_dataset.csv", [olist_row("olist_customers_dataset.csv", customer_city="curitiba")])
    write_rows(tmp_path / "olist_order_reviews_dataset.csv", [olist_row("olist_order_reviews_dataset.csv", review_score="1")])
    fail_on_table(monkeypatch, "reviews", LoadError("disk full"))

    assert run_olist(tmp_path).status is RunStatus.FAILED
    assert rows_of(db, "SELECT * FROM olist.customers") == before
    assert scalar(db, "SELECT count(*) FROM ops.record_history") == 0


def test_rejections_from_a_rolled_back_run_are_not_kept(run_hotel, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_hotel(tmp_path, [hotel_row(adr="-1"), hotel_row()])
    fail_on_table(monkeypatch, "hotel_bookings", LoadError("disk full"))

    run_hotel(tmp_path)

    assert scalar(db, "SELECT count(*) FROM ops.rejected_records") == 0


def test_transient_failure_retries_and_succeeds_without_duplicates(run_hotel, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_hotel(tmp_path, [hotel_row()])
    calls = fail_on_table(monkeypatch, "hotel_bookings", TransientDatabaseError("connection dropped"), times=2)

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert result.attempts == 3
    assert len(calls) == 2
    assert result.metrics.load_total("inserted") == 1
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1
    assert scalar(db, "SELECT attempts FROM ops.pipeline_runs") == 3


def test_retries_stop_at_the_configured_limit(run_hotel, db: Engine, tmp_path: Path, monkeypatch, settings: Settings) -> None:
    write_hotel(tmp_path, [hotel_row()])
    calls = fail_on_table(monkeypatch, "hotel_bookings", TransientDatabaseError("connection dropped"))

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert len(calls) == settings.pipeline.max_retries + 1
    assert "connection dropped" in result.failure_reason


def test_permanent_failure_is_not_retried(run_hotel, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_hotel(tmp_path, [hotel_row()])
    calls = fail_on_table(monkeypatch, "hotel_bookings", LoadError("constraint violated"))

    run_hotel(tmp_path)

    assert len(calls) == 1


def test_real_database_constraint_failure_rolls_back_and_is_not_retried(run_hotel, db: Engine, tmp_path: Path) -> None:
    """No mocks: PostgreSQL itself refuses a row midway through a file."""
    write_hotel(tmp_path, [hotel_row(adr="10.00")])
    run_hotel(tmp_path)
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE hotel.hotel_bookings ADD CONSTRAINT adr_limit CHECK (adr < 500)"))
    write_hotel(tmp_path, [hotel_row(adr="20.00"), hotel_row(adr="600.00")])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert result.attempts == 0
    assert "adr_limit" in result.failure_reason
    assert float(scalar(db, "SELECT adr FROM hotel.hotel_bookings")) == 10.0  # the first row was not updated either
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1


def test_database_connection_failure_fails_cleanly_and_writes_a_report(settings: Settings, hotel_contract: DatasetContract, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    unreachable = settings.database.model_copy(update={"port": 1})
    engine = create_db_engine(unreachable)

    result = Pipeline(settings, hotel_contract, engine, RunOptions(source_dir=tmp_path)).run()
    engine.dispose()

    assert result.status is RunStatus.FAILED
    assert result.failure_reason
    assert "connect" in result.failure_reason.lower()
    assert result.report_path.is_file()


def test_reconciliation_catches_a_transformation_bug_and_publishes_nothing(run_hotel, db: Engine, tmp_path: Path, monkeypatch) -> None:
    """A deliberate bug corrupts ADR. The independent source total must expose it."""
    write_hotel(tmp_path, [hotel_row()])
    original = HotelTransformer.transform_hotel_bookings

    def corrupted(self, frame):
        result = original(self, frame)
        result["adr"] = result["adr"] * 2
        return result

    monkeypatch.setattr(HotelTransformer, "transform_hotel_bookings", corrupted)

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "adr_total" in result.failure_reason
    assert scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'hotel'") == 0


def test_second_concurrent_run_is_refused(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    run_hotel(tmp_path)

    with dataset_lock(db, "hotel_bookings"):
        result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "in progress" in result.failure_reason


def test_lock_for_one_dataset_does_not_block_another(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)

    with dataset_lock(db, "hotel_bookings"):
        assert run_olist(tmp_path).status is RunStatus.SUCCESS


def test_run_left_running_by_a_crash_is_marked_abandoned(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    first = run_hotel(tmp_path)
    with db.begin() as conn:
        conn.execute(text("UPDATE ops.pipeline_runs SET status = 'running', finished_at = NULL WHERE run_id = :r"), {"r": first.run_id})

    run_hotel(tmp_path)

    assert scalar(db, "SELECT status FROM ops.pipeline_runs WHERE run_id = :r", r=first.run_id) == "abandoned"


def test_reused_run_id_does_not_overwrite_history(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    first = run_hotel(tmp_path)

    second = run_hotel(tmp_path, run_id=first.run_id)

    assert second.status is RunStatus.FAILED
    assert scalar(db, "SELECT status FROM ops.pipeline_runs WHERE run_id = :r", r=first.run_id) == "success"
    assert scalar(db, "SELECT count(*) FROM ops.pipeline_runs") == 1
