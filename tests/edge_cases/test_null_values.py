"""Required nulls are rejected. Optional nulls stay null, never filled with a guess."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from src.core.constants import RunStatus
from tests.conftest import rows_of, scalar
from tests.helpers import hotel_row, olist_row, write_hotel, write_olist

ORDERS = "olist_orders_dataset.csv"


@pytest.mark.parametrize("column", ["hotel", "lead_time", "adr", "reservation_status", "reservation_status_date"])
def test_null_in_required_hotel_column_is_quarantined(run_hotel, db: Engine, tmp_path: Path, column: str) -> None:
    write_hotel(tmp_path, [hotel_row(**{column: ""}), hotel_row()])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert f"missing_required:{column}" in scalar(db, "SELECT rejection_reason FROM ops.rejected_records")
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1


def test_null_primary_key_is_rejected_and_counted_as_a_missing_key(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_orders_dataset=[olist_row(ORDERS), olist_row(ORDERS, order_id="")])

    result = run_olist(tmp_path)

    assert result.metrics.table("orders").missing_keys == 1
    assert scalar(db, "SELECT count(*) FROM olist.orders") == 1
    assert scalar(db, "SELECT rejection_reason FROM ops.rejected_records") == "missing_required:order_id"


def test_undelivered_order_nulls_are_preserved(run_olist, db: Engine, tmp_path: Path) -> None:
    """2,965 real orders have no delivery date; that is information, not an error."""
    write_olist(tmp_path, olist_orders_dataset=[olist_row(
        ORDERS, order_status="shipped", order_delivered_customer_date="")])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS

    row = rows_of(db, "SELECT order_delivered_customer_date, delivery_days, is_late_delivery FROM olist.orders")[0]
    assert all(value is None for value in row.values())


def test_delivered_status_without_a_date_is_flagged(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_orders_dataset=[olist_row(ORDERS, order_delivered_customer_date="")])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM olist.orders") == "delivered_without_date"


def test_null_optional_hotel_columns_stay_null(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(country="", agent="", company="", children="")])

    assert run_hotel(tmp_path).status is RunStatus.SUCCESS

    row = rows_of(db, "SELECT country, agent, company, children FROM hotel.hotel_bookings")[0]
    assert all(value is None for value in row.values())
