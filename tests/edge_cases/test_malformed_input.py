"""Records that cannot be parsed, or hold values that cannot be used."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from src.core.constants import RunStatus
from tests.conftest import rows_of, scalar
from tests.helpers import HOTEL_ROW, hotel_row, olist_row, write_hotel, write_olist


def test_row_with_wrong_field_count_is_quarantined_and_the_rest_load(run_hotel, db: Engine, tmp_path: Path) -> None:
    path = write_hotel(tmp_path, [hotel_row(), hotel_row()])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("too,few,fields\n")
        handle.write(",".join(hotel_row().values()) + "\n")

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert result.metrics.table("hotel_bookings").malformed == 1
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 3
    rejected = rows_of(db, "SELECT * FROM ops.rejected_records")[0]
    assert rejected["rejection_reason"] == "malformed_row"
    assert rejected["source_row_number"] == 3
    assert rejected["original_record"]["_raw"] == ["too", "few", "fields"]


def test_a_file_of_only_malformed_rows_loads_nothing_but_succeeds_with_rejects(run_hotel, db: Engine, tmp_path: Path) -> None:
    (tmp_path / "hotel_bookings.csv").write_text(",".join(HOTEL_ROW) + "\nbad\nworse,row\n", encoding="utf-8")

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert result.metrics.total("rejected") == 2
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 0


@pytest.mark.parametrize(
    ("column", "value", "rule"),
    [
        ("lead_time", "soon", "invalid_type:lead_time"),
        ("adr", "12,50", "invalid_type:adr"),
        ("reservation_status_date", "08/03/2016", "invalid_type:reservation_status_date"),
        ("arrival_date_month", "Smarch", "invalid_arrival_date"),
        ("is_canceled", "2", "invalid_is_canceled"),
    ],
)
def test_unusable_values_are_rejected_with_the_right_rule(run_hotel, db: Engine, tmp_path: Path, column: str, value: str, rule: str) -> None:
    write_hotel(tmp_path, [hotel_row(**{column: value})])

    run_hotel(tmp_path)

    assert rule in scalar(db, "SELECT rejection_reason FROM ops.rejected_records")
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 0


def test_nul_character_is_rejected_rather_than_crashing_the_load(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(market_segment="Dir\x00ect"), hotel_row()])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS, result.failure_reason
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1


def test_sql_in_a_value_is_stored_as_plain_text(run_olist, db: Engine, tmp_path: Path) -> None:
    hostile = "'); DROP TABLE olist.orders; --"
    write_olist(tmp_path, olist_order_reviews_dataset=[olist_row("olist_order_reviews_dataset.csv", review_comment_message=hostile)])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT review_comment_message FROM olist.reviews") == hostile
    assert scalar(db, "SELECT count(*) FROM olist.orders") == 1


def test_tabs_and_backslashes_survive_copy(run_olist, db: Engine, tmp_path: Path) -> None:
    value = "a\tb\\nc\\"
    write_olist(tmp_path, olist_order_reviews_dataset=[olist_row("olist_order_reviews_dataset.csv", review_comment_message=value)])

    assert run_olist(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT review_comment_message FROM olist.reviews") == value


def test_invalid_reference_key_is_rejected(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[
        olist_row("olist_order_items_dataset.csv"),
        olist_row("olist_order_items_dataset.csv", order_item_id="2", seller_id="ghost"),
    ])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert scalar(db, "SELECT count(*) FROM olist.order_items") == 1
    assert scalar(db, "SELECT rejection_reason FROM ops.rejected_records") == "fk_violation:seller_id"
