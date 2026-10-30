"""Each duplicate policy, end to end."""

from pathlib import Path

from sqlalchemy import Engine

from src.core.constants import RunStatus
from tests.conftest import rows_of, scalar
from tests.helpers import hotel_row, olist_row, write_hotel, write_olist

CUSTOMERS = "olist_customers_dataset.csv"


def test_duplicate_primary_key_keeps_the_first_row_and_quarantines_the_second(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_customers_dataset=[
        olist_row(CUSTOMERS), olist_row(CUSTOMERS, customer_city="other city")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert scalar(db, "SELECT customer_city FROM olist.customers") == "sao paulo"
    rejected = rows_of(db, "SELECT * FROM ops.rejected_records")[0]
    assert rejected["rejection_reason"] == "duplicate_key"
    assert rejected["source_row_number"] == 2
    assert rejected["original_record"]["customer_city"] == "other city"
    assert result.metrics.table("customers").duplicate_keys == 1


def test_duplicate_key_across_batches_is_still_caught(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_customers_dataset=[olist_row(CUSTOMERS), olist_row(CUSTOMERS)])

    result = run_olist(tmp_path, batch_size=1)

    assert result.metrics.table("customers").duplicate_keys == 1
    assert scalar(db, "SELECT count(*) FROM olist.customers") == 1


def test_exact_hotel_copies_all_load_and_are_flagged(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(), hotel_row(), hotel_row(adr="1.00")])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.table("hotel_bookings").duplicates_flagged == 2
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 4
    assert scalar(db, "SELECT count(DISTINCT record_id) FROM hotel.hotel_bookings") == 4
    assert scalar(db, "SELECT count(DISTINCT record_fingerprint) FROM hotel.hotel_bookings") == 2
    assert [row["duplicate_occurrence"] for row in rows_of(
        db, "SELECT duplicate_occurrence FROM hotel.hotel_bookings ORDER BY source_row_number")] == [1, 2, 3, 1]


def test_hotel_copies_across_batches_are_numbered_correctly(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(), hotel_row()])

    run_hotel(tmp_path, batch_size=2)

    assert scalar(db, "SELECT max(duplicate_occurrence) FROM hotel.hotel_bookings") == 3
