"""Sequences of runs that once gave wrong results, found by an independent code review."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from src.core.config import Settings
from src.core.constants import RunStatus
from src.core.contract import DatasetContract
from src.pipelines.pipeline import Pipeline, RunOptions
from tests.conftest import rows_of, scalar
from tests.helpers import HOTEL_ROW, hotel_row, olist_row, write_hotel, write_olist, write_rows

ORDERS = "olist_orders_dataset.csv"
ITEMS = "olist_order_items_dataset.csv"
PAYMENTS = "olist_order_payments_dataset.csv"
GEOLOCATION = "olist_geolocation_dataset.csv"


def aggregate_status(result, table: str, name: str) -> dict:
    aggregates = result.reconciliation["tables"][table]["aggregates"]
    return next(aggregate for aggregate in aggregates if aggregate["name"] == name)


# ---- snapshot safety --------------------------------------------------------------------


def test_file_where_every_row_is_rejected_cannot_wipe_its_bookings(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(adr="50.00")])
    run_hotel(tmp_path)

    (tmp_path / "hotel_bookings.csv").write_text(",".join(HOTEL_ROW) + "\ntruncated,row\n", encoding="utf-8")
    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "truncated" in result.failure_reason
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 2
    assert scalar(db, "SELECT count(*) FROM ops.record_history") == 0


def test_file_losing_most_of_its_rows_is_refused_until_a_full_refresh(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr=f"{n}.00") for n in range(1, 11)])
    run_hotel(tmp_path)

    write_hotel(tmp_path, [hotel_row(adr="1.00")])

    assert run_hotel(tmp_path).status is RunStatus.FAILED
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 10

    assert run_hotel(tmp_path, full_refresh=True).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1


def test_full_refresh_with_a_single_input_is_refused_before_anything_is_dropped(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    result = run_olist(input_files=(tmp_path / ITEMS,), full_refresh=True)

    assert result.status is RunStatus.FAILED
    assert "--input" in result.failure_reason
    assert scalar(db, "SELECT count(*) FROM olist.orders") == 1


# ---- quarantine against corrections -----------------------------------------------------


def _load_with_payment_for_missing_order(run_olist, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_payments_dataset=[
        olist_row(PAYMENTS), olist_row(PAYMENTS, order_id="o9", payment_value="10.00")])
    assert run_olist(tmp_path).status is RunStatus.SUCCESS_WITH_REJECTS


def test_stale_rejection_cannot_overwrite_a_corrected_value(run_olist, db: Engine, tmp_path: Path) -> None:
    _load_with_payment_for_missing_order(run_olist, tmp_path)

    # The parent arrives along with a corrected file, which fixes the amount to 12.
    write_rows(tmp_path / ORDERS, [olist_row(ORDERS), olist_row(ORDERS, order_id="o9")])
    write_rows(tmp_path / PAYMENTS, [olist_row(PAYMENTS), olist_row(PAYMENTS, order_id="o9", payment_value="12.00")])
    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert float(scalar(db, "SELECT payment_value FROM olist.payments WHERE order_id = 'o9'")) == 12.0
    assert scalar(db, "SELECT resolution FROM ops.rejected_records") == "superseded"


def test_row_removed_by_a_correction_does_not_return_when_its_parent_arrives(run_olist, db: Engine, tmp_path: Path) -> None:
    _load_with_payment_for_missing_order(run_olist, tmp_path)

    write_rows(tmp_path / ORDERS, [olist_row(ORDERS), olist_row(ORDERS, order_id="o9")])
    write_rows(tmp_path / PAYMENTS, [olist_row(PAYMENTS)])
    run_olist(tmp_path)

    assert scalar(db, "SELECT count(*) FROM olist.payments WHERE order_id = 'o9'") == 0


def test_recovery_never_overwrites_a_row_that_arrived_from_another_file(run_olist, db: Engine, tmp_path: Path) -> None:
    _load_with_payment_for_missing_order(run_olist, tmp_path)

    # The rejection is in the base file. A separate file delivers the parent and a newer value.
    write_rows(tmp_path / "olist_orders_dataset_delta.csv", [olist_row(ORDERS, order_id="o9")])
    write_rows(tmp_path / "olist_order_payments_dataset_delta.csv", [olist_row(PAYMENTS, order_id="o9", payment_value="12.00")])
    run_olist(tmp_path)

    assert float(scalar(db, "SELECT payment_value FROM olist.payments WHERE order_id = 'o9'")) == 12.0
    assert scalar(db, "SELECT resolution FROM ops.rejected_records") == "superseded"


def test_full_refresh_closes_rejections_from_a_file_that_no_longer_exists(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    delta = write_rows(tmp_path / "olist_order_payments_dataset_delta.csv", [olist_row(PAYMENTS, order_id="o9")])
    run_olist(tmp_path)
    delta.unlink()

    write_rows(tmp_path / ORDERS, [olist_row(ORDERS), olist_row(ORDERS, order_id="o9")])
    run_olist(tmp_path, full_refresh=True)

    assert scalar(db, "SELECT count(*) FROM olist.payments WHERE order_id = 'o9'") == 0
    assert scalar(db, "SELECT resolution FROM ops.rejected_records") == "superseded"


def test_relaxed_rule_closes_the_rejection_of_a_row_that_now_loads(settings: Settings, hotel_contract: DatasetContract, db: Engine, tmp_path: Path) -> None:
    table = hotel_contract.tables[0]
    stricter = hotel_contract.model_copy(update={"tables": (table.model_copy(update={
        "rules": (*table.rules, table.rules[0].model_copy(update={"name": "too_expensive", "max": 500.0})),
    }),)})
    write_hotel(tmp_path, [hotel_row(adr="600.00"), hotel_row(), hotel_row()])
    assert Pipeline(settings, stricter, db, RunOptions(source_dir=tmp_path)).run().status is RunStatus.SUCCESS_WITH_REJECTS

    relaxed = Pipeline(settings, hotel_contract, db, RunOptions(source_dir=tmp_path)).run()

    assert relaxed.status is RunStatus.SUCCESS
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 3
    assert scalar(db, "SELECT resolution FROM ops.rejected_records") == "superseded"


def test_row_rejected_again_after_a_new_version_stays_open(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="-1"), hotel_row()])
    run_hotel(tmp_path, backfill=True)

    run_hotel(tmp_path, backfill=True)

    assert scalar(db, "SELECT count(*) FROM ops.rejected_records WHERE resolved_at IS NULL") == 1


def test_row_rejected_and_recovered_in_one_run_counts_as_accepted_only(run_olist, db: Engine, tmp_path: Path) -> None:
    """Order o9 waits for its customer. When the customer arrives, the run also delivers an item
    for o9: it is rejected because o9 is still quarantined, then recovered once o9 is."""
    write_olist(tmp_path, olist_orders_dataset=[olist_row(ORDERS), olist_row(ORDERS, order_id="o9", customer_id="c9")])
    assert run_olist(tmp_path).status is RunStatus.SUCCESS_WITH_REJECTS

    customers = "olist_customers_dataset.csv"
    write_rows(tmp_path / "olist_customers_dataset_delta.csv", [olist_row(customers, customer_id="c9")])
    write_rows(tmp_path / "olist_order_items_dataset_delta.csv", [olist_row(ITEMS, order_id="o9")])
    result = run_olist(tmp_path)

    items = result.metrics.table("order_items")
    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert (items.rejected, items.valid, items.recovered_same_run, items.reprocessed) == (0, 1, 1, 0)
    assert items.rule_failures == {}
    assert result.metrics.table("orders").reprocessed == 1
    assert scalar(db, "SELECT count(*) FROM olist.order_items WHERE order_id = 'o9'") == 1


# ---- reconciliation honesty -------------------------------------------------------------


def test_partial_redelivery_is_reported_as_not_comparable_not_as_a_pass(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[olist_row(ITEMS), olist_row(ITEMS, order_item_id="2")])
    run_olist(tmp_path)

    # A new file re-delivers one existing row, identical, so it stays attributed to the first file.
    write_rows(tmp_path / "olist_order_items_dataset_delta.csv", [olist_row(ITEMS)])
    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    aggregate = aggregate_status(result, "order_items", "price_total")
    assert aggregate["status"] == "not_comparable"
    assert "attributed" in aggregate["reason"]


def test_same_key_twice_across_files_in_one_run_does_not_block_the_load(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[olist_row(ITEMS, price="10.00")])
    write_rows(tmp_path / "olist_order_items_dataset_z.csv", [olist_row(ITEMS, price="12.00")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert float(scalar(db, "SELECT price FROM olist.order_items")) == 12.0
    assert aggregate_status(result, "order_items", "price_total")["status"] == "not_comparable"


def test_corrected_file_that_drops_a_row_does_not_block_the_load(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[olist_row(ITEMS), olist_row(ITEMS, order_item_id="2")])
    run_olist(tmp_path)

    write_rows(tmp_path / ITEMS, [olist_row(ITEMS, price="99.00")])
    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert aggregate_status(result, "order_items", "price_total")["status"] == "not_comparable"


def test_ordinary_rerun_still_compares_and_matches(run_olist, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    result = run_olist(tmp_path, backfill=True)

    assert aggregate_status(result, "order_items", "price_total")["status"] == "match"


def test_counts_add_up_for_a_mixed_rerun(run_olist, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[olist_row(ITEMS), olist_row(ITEMS, order_item_id="2")])
    run_olist(tmp_path)

    write_rows(tmp_path / ITEMS, [olist_row(ITEMS), olist_row(ITEMS, order_item_id="2", price="5.00"), olist_row(ITEMS, order_item_id="3")])
    load = run_olist(tmp_path).metrics.table("order_items").load

    assert (load.inserted, load.updated, load.skipped) == (1, 1, 1)


# ---- values and flags -------------------------------------------------------------------


def test_more_decimals_than_stored_are_rejected_not_rounded(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_order_items_dataset=[
        olist_row(ITEMS), olist_row(ITEMS, order_item_id="2", price="10.125", freight_value="1.125")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS, result.failure_reason
    assert "invalid_type:price" in scalar(db, "SELECT rejection_reason FROM ops.rejected_records")
    assert scalar(db, "SELECT count(*) FROM olist.order_items") == 1


def test_changed_warning_rule_refreshes_flags_on_unchanged_rows(settings: Settings, hotel_contract: DatasetContract, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="600.00")])
    Pipeline(settings, hotel_contract, db, RunOptions(source_dir=tmp_path)).run()
    assert scalar(db, "SELECT dq_warnings FROM hotel.hotel_bookings") is None

    table = hotel_contract.tables[0]
    rule = next(r for r in table.rules if r.name == "high_adr").model_copy(update={"name": "over_500", "max": 500.0})
    changed = hotel_contract.model_copy(update={"tables": (table.model_copy(update={"rules": (*table.rules, rule)}),)})
    result = Pipeline(settings, changed, db, RunOptions(source_dir=tmp_path)).run()

    assert result.metrics.load_total("updated") == 1
    assert scalar(db, "SELECT dq_warnings FROM hotel.hotel_bookings") == "over_500"


@pytest.mark.parametrize(
    ("column", "rule"),
    [("stays_in_week_nights", "implausible_week_nights"), ("stays_in_weekend_nights", "implausible_weekend_nights"),
     ("lead_time", "implausible_lead_time")],
)
def test_extreme_value_rejects_one_row_instead_of_aborting_the_run(run_hotel, db: Engine, tmp_path: Path, column: str, rule: str) -> None:
    write_hotel(tmp_path, [hotel_row(**{column: "2147483647"}), hotel_row()])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS, result.failure_reason
    assert rule in scalar(db, "SELECT rejection_reason FROM ops.rejected_records")
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1


# ---- multiple files, and outputs --------------------------------------------------------


def test_collapse_table_refuses_a_second_source_file(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    write_rows(tmp_path / "olist_geolocation_dataset_extra.csv", [olist_row(GEOLOCATION, geolocation_city="x")])

    result = run_olist(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "one source file" in result.failure_reason


def test_failure_writing_output_files_does_not_change_a_committed_outcome(settings: Settings, hotel_contract: DatasetContract, db: Engine, tmp_path: Path, monkeypatch) -> None:
    write_hotel(tmp_path, [hotel_row()])

    def locked(self) -> None:
        raise PermissionError("file is open in another program")

    monkeypatch.setattr(Pipeline, "_write_processed_samples", locked)
    result = Pipeline(settings, hotel_contract, db, RunOptions(source_dir=tmp_path)).run()

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.load_total("inserted") == 1
    assert scalar(db, "SELECT status FROM ops.pipeline_runs") == "success"


def test_identical_files_with_different_names_each_keep_their_own_rejection(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="-1"), hotel_row()])
    write_hotel(tmp_path, [hotel_row(adr="-1"), hotel_row()], name="hotel_bookings_copy.csv")

    run_hotel(tmp_path)

    assert {row["source_file"] for row in rows_of(db, "SELECT source_file FROM ops.rejected_records")} == {
        "hotel_bookings.csv", "hotel_bookings_copy.csv"}
