"""Empty batches, empty files, and missing files."""

from pathlib import Path

from sqlalchemy import Engine

from src.core.constants import RunStatus
from tests.conftest import rows_of, scalar
from tests.helpers import HOTEL_ROW, hotel_row, write_hotel


def test_zero_byte_file_fails_the_run_with_a_clear_reason(run_hotel, db: Engine, tmp_path: Path) -> None:
    (tmp_path / "hotel_bookings.csv").write_bytes(b"")

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "empty" in result.failure_reason
    assert scalar(db, "SELECT failure_reason FROM ops.pipeline_runs") == result.failure_reason
    assert scalar(db, "SELECT count(*) FROM ops.processed_files") == 0


def test_header_only_file_is_an_empty_batch_not_a_failure(run_hotel, db: Engine, tmp_path: Path) -> None:
    (tmp_path / "hotel_bookings.csv").write_text(",".join(HOTEL_ROW) + "\n", encoding="utf-8")

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.total("input") == 0
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 0


def test_empty_batch_does_not_delete_a_loaded_snapshot(run_hotel, db: Engine, tmp_path: Path) -> None:
    """A truncated delivery must not be read as 'all bookings were removed'."""
    write_hotel(tmp_path, [hotel_row(), hotel_row(adr="50.00")])
    run_hotel(tmp_path)

    (tmp_path / "hotel_bookings.csv").write_text(",".join(HOTEL_ROW) + "\n", encoding="utf-8")
    result = run_hotel(tmp_path)

    assert result.metrics.load_total("deleted") == 0
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 2


def test_missing_source_file_fails_and_names_the_pattern(run_hotel, db: Engine, tmp_path: Path) -> None:
    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "hotel_bookings*.csv" in result.failure_reason


def test_one_missing_olist_file_publishes_nothing(run_olist, db: Engine, tmp_path: Path) -> None:
    from tests.helpers import write_olist

    write_olist(tmp_path)
    (tmp_path / "olist_order_reviews_dataset.csv").unlink()

    result = run_olist(tmp_path)

    assert result.status is RunStatus.FAILED
    # The tables were created in the same transaction, so even they rolled back.
    assert scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'olist'") == 0
    assert result.metrics.load_total("inserted") == 0
    assert rows_of(db, "SELECT * FROM ops.processed_files") == []
