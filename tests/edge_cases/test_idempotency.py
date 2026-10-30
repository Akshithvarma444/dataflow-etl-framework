"""Reruns, corrected files, backfills, late arrivals, and out-of-order delivery."""

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine

from src.core.config import Settings
from src.core.constants import RunStatus
from src.core.contract import DatasetContract
from src.generators.data_generator import generate_dataset
from src.pipelines.pipeline import Pipeline, RunOptions
from tests.conftest import rows_of, scalar
from tests.helpers import hotel_row, olist_row, write_hotel, write_olist, write_rows

ORDERS = "olist_orders_dataset.csv"
ITEMS = "olist_order_items_dataset.csv"
CUSTOMERS = "olist_customers_dataset.csv"


def snapshot(db: Engine, table: str) -> list:
    return rows_of(db, f"SELECT * FROM {table} ORDER BY 1, 2")


# ---- same file again -------------------------------------------------------------------


def test_same_file_creates_a_new_run_but_changes_no_target_row(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(adr="-1")])
    first = run_hotel(tmp_path)
    before = snapshot(db, "hotel.hotel_bookings")

    second = run_hotel(tmp_path)

    assert first.status is RunStatus.SUCCESS_WITH_REJECTS
    assert second.status is RunStatus.SKIPPED
    assert second.metrics.table("hotel_bookings").files_skipped == ["hotel_bookings.csv"]
    assert snapshot(db, "hotel.hotel_bookings") == before
    runs = rows_of(db, "SELECT run_id, status FROM ops.pipeline_runs")
    assert {row["run_id"] for row in runs} == {first.run_id, second.run_id}
    assert scalar(db, "SELECT count(*) FROM ops.rejected_records") == 1


def test_backfill_of_identical_data_reports_everything_unchanged(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)
    before = snapshot(db, "olist.orders")

    result = run_olist(tmp_path, backfill=True)

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.load_total("inserted") == 0
    assert result.metrics.load_total("updated") == 0
    assert result.metrics.load_total("skipped") == 9
    assert snapshot(db, "olist.orders") == before
    assert scalar(db, "SELECT count(*) FROM ops.record_history") == 0


def test_backfill_does_not_duplicate_quarantine_rows(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="-1"), hotel_row()])
    first = run_hotel(tmp_path)

    second = run_hotel(tmp_path, backfill=True)

    rows = rows_of(db, "SELECT run_id, last_seen_run_id FROM ops.rejected_records")
    assert len(rows) == 1
    assert rows[0]["run_id"] == first.run_id
    assert rows[0]["last_seen_run_id"] == second.run_id


def test_full_refresh_rebuilds_to_the_same_content(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(adr="50.00")])
    run_hotel(tmp_path)
    before = rows_of(db, "SELECT record_id, adr, row_hash FROM hotel.hotel_bookings ORDER BY 1")

    result = run_hotel(tmp_path, full_refresh=True)

    assert result.metrics.load_total("inserted") == 2
    assert rows_of(db, "SELECT record_id, adr, row_hash FROM hotel.hotel_bookings ORDER BY 1") == before


def test_dry_run_reports_real_counts_and_changes_nothing(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(), hotel_row(adr="-1")])

    result = run_hotel(tmp_path, dry_run=True)

    assert (result.metrics.load_total("inserted"), result.metrics.total("rejected")) == (1, 1)
    assert scalar(db, "SELECT count(*) FROM ops.rejected_records") == 0
    assert scalar(db, "SELECT count(*) FROM ops.processed_files") == 0
    assert scalar(db, "SELECT run_mode FROM ops.pipeline_runs") == "dry_run"
    assert run_hotel(tmp_path).status is RunStatus.SUCCESS_WITH_REJECTS  # not recorded as processed


# ---- changed files ---------------------------------------------------------------------


def test_changed_olist_file_updates_only_changed_rows_and_keeps_history(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    write_rows(tmp_path / CUSTOMERS, [olist_row(CUSTOMERS, customer_city="curitiba"), olist_row(CUSTOMERS, customer_id="c2")])
    result = run_olist(tmp_path)

    customers = result.metrics.table("customers")
    assert (customers.load.inserted, customers.load.updated) == (1, 1)
    assert customers.files_processed == [CUSTOMERS]
    assert result.metrics.table("orders").files_skipped == [ORDERS]
    assert scalar(db, "SELECT customer_city FROM olist.customers WHERE customer_id = 'c1'") == "curitiba"
    history = rows_of(db, "SELECT * FROM ops.record_history")
    assert len(history) == 1
    assert history[0]["previous_record"]["customer_city"] == "sao paulo"


def test_changed_file_processes_while_unchanged_parent_files_still_satisfy_references(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    write_rows(tmp_path / ITEMS, [olist_row(ITEMS), olist_row(ITEMS, order_item_id="2")])
    result = run_olist(tmp_path)

    assert result.metrics.total("rejected") == 0
    assert scalar(db, "SELECT count(*) FROM olist.order_items") == 2


def test_order_summary_reflects_changes_from_any_table(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)

    write_rows(tmp_path / ITEMS, [olist_row(ITEMS), olist_row(ITEMS, order_item_id="2", price="10.00", freight_value="0.00")])
    run_olist(tmp_path)

    row = rows_of(db, "SELECT * FROM olist.order_summary")[0]
    assert (row["item_count"], float(row["items_total"])) == (2, 39.99)


def test_restoring_a_file_to_its_earlier_content_is_a_change(run_olist, db: Engine, tmp_path: Path) -> None:
    """A checksum seen before must not mean skip if a newer version was loaded since."""
    write_olist(tmp_path)
    run_olist(tmp_path)
    original = (tmp_path / CUSTOMERS).read_bytes()

    write_rows(tmp_path / CUSTOMERS, [olist_row(CUSTOMERS, customer_city="curitiba")])
    run_olist(tmp_path)

    (tmp_path / CUSTOMERS).write_bytes(original)
    result = run_olist(tmp_path)

    assert result.metrics.table("customers").load.updated == 1
    assert scalar(db, "SELECT customer_city FROM olist.customers") == "sao paulo"


def test_contract_change_reprocesses_an_unchanged_file(settings: Settings, hotel_contract: DatasetContract, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="600.00")])
    assert Pipeline(settings, hotel_contract, db, RunOptions(source_dir=tmp_path)).run().status is RunStatus.SUCCESS

    table = hotel_contract.tables[0]
    stricter_rule = table.rules[0].model_copy(update={"max": 500.0, "name": "adr_out_of_range"})
    stricter = hotel_contract.model_copy(update={
        "tables": (table.model_copy(update={"rules": (*table.rules, stricter_rule)}),),
    })
    result = Pipeline(settings, stricter, db, RunOptions(source_dir=tmp_path)).run()

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    assert result.metrics.total("rejected") == 1


def test_corrected_row_in_a_rerun_resolves_nothing_silently(run_hotel, db: Engine, tmp_path: Path) -> None:
    """The old rejection stays on record as history of what was received."""
    write_hotel(tmp_path, [hotel_row(adr="-1")])
    run_hotel(tmp_path)

    write_hotel(tmp_path, [hotel_row(adr="10.00")])
    run_hotel(tmp_path)

    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1
    assert scalar(db, "SELECT count(*) FROM ops.rejected_records") == 1


def test_hotel_snapshot_with_fewer_rows_removes_and_archives_the_rest(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="1.00"), hotel_row(adr="2.00"), hotel_row(adr="3.00")])
    run_hotel(tmp_path)

    write_hotel(tmp_path, [hotel_row(adr="1.00")])
    result = run_hotel(tmp_path)

    assert result.metrics.load_total("deleted") == 2
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1
    assert scalar(db, "SELECT count(*) FROM ops.record_history WHERE change_type = 'delete'") == 2


def test_hotel_corrected_value_does_not_create_a_second_booking(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="1.00")])
    run_hotel(tmp_path)

    write_hotel(tmp_path, [hotel_row(adr="2.00")])
    result = run_hotel(tmp_path)

    assert result.metrics.load_total("updated") == 1
    assert float(scalar(db, "SELECT adr FROM hotel.hotel_bookings")) == 2.0
    assert scalar(db, "SELECT count(*) FROM ops.record_history WHERE change_type = 'update'") == 1


def test_separate_hotel_files_do_not_affect_each_others_rows(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    run_hotel(tmp_path)

    write_hotel(tmp_path, [hotel_row(adr="9.00")], name="hotel_bookings_late.csv")
    result = run_hotel(tmp_path)

    assert result.metrics.load_total("deleted") == 0
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 2


# ---- late and out-of-order data --------------------------------------------------------


def test_late_arriving_parent_recovers_its_quarantined_child(run_olist, db: Engine, tmp_path: Path) -> None:
    """The order line arrives first, is quarantined, and loads when its order arrives."""
    write_olist(tmp_path)
    run_olist(tmp_path)

    late_item = olist_row(ITEMS, order_id="late_order")
    write_rows(tmp_path / "olist_order_items_dataset_delta.csv", [late_item])
    first = run_olist(tmp_path)
    assert first.status is RunStatus.SUCCESS_WITH_REJECTS
    assert scalar(db, "SELECT count(*) FROM olist.order_items WHERE order_id = 'late_order'") == 0
    assert scalar(db, "SELECT resolved_at FROM ops.rejected_records") is None

    write_rows(tmp_path / "olist_orders_dataset_delta.csv", [olist_row(ORDERS, order_id="late_order")])
    second = run_olist(tmp_path)

    assert second.status is RunStatus.SUCCESS
    assert second.metrics.table("order_items").reprocessed == 1
    assert scalar(db, "SELECT count(*) FROM olist.order_items WHERE order_id = 'late_order'") == 1
    resolved = rows_of(db, "SELECT resolved_at, resolved_by_run_id FROM ops.rejected_records")[0]
    assert resolved["resolved_at"] is not None
    assert resolved["resolved_by_run_id"] == second.run_id
    assert scalar(db, "SELECT item_count FROM olist.order_summary WHERE order_id = 'late_order'") == 1


def test_child_rejected_for_another_reason_too_is_not_recovered(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path)
    run_olist(tmp_path)
    write_rows(tmp_path / "olist_order_items_dataset_delta.csv", [olist_row(ITEMS, order_id="late_order", price="-1")])
    run_olist(tmp_path)

    write_rows(tmp_path / "olist_orders_dataset_delta.csv", [olist_row(ORDERS, order_id="late_order")])
    result = run_olist(tmp_path)

    assert result.metrics.table("order_items").reprocessed == 0
    assert scalar(db, "SELECT count(*) FROM olist.order_items WHERE order_id = 'late_order'") == 0


def test_late_arriving_hotel_rows_add_without_disturbing_loaded_rows(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(arrival_date_year="2017")])
    run_hotel(tmp_path)
    before = rows_of(db, "SELECT record_id, last_run_id FROM hotel.hotel_bookings")

    write_hotel(tmp_path, [hotel_row(arrival_date_year="2015", reservation_status_date="2015-03-08")], name="hotel_bookings_late.csv")
    result = run_hotel(tmp_path)

    assert result.metrics.load_total("inserted") == 1
    assert result.metrics.table("hotel_bookings").files_skipped == ["hotel_bookings.csv"]
    assert rows_of(db, "SELECT record_id, last_run_id FROM hotel.hotel_bookings WHERE arrival_date_year = 2017") == before
    assert scalar(db, "SELECT min(arrival_date) FROM hotel.hotel_bookings").year == 2015


def test_older_order_version_delivered_late_does_not_overwrite_the_newer_one(run_olist, db: Engine, tmp_path: Path) -> None:
    write_olist(tmp_path, olist_orders_dataset=[olist_row(ORDERS)])
    run_olist(tmp_path)

    older = olist_row(ORDERS, order_status="invoiced", order_approved_at="2017-10-02 11:07:15",
                      order_delivered_carrier_date="", order_delivered_customer_date="")
    write_rows(tmp_path / "olist_orders_dataset_resend.csv", [older])
    result = run_olist(tmp_path)

    assert result.status is RunStatus.SUCCESS, result.failure_reason
    assert result.metrics.table("orders").load.stale == 1
    assert scalar(db, "SELECT order_status FROM olist.orders") == "delivered"
    assert scalar(db, "SELECT count(*) FROM ops.record_history") == 0
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["totals"]["stale"] == 1


def test_newer_order_version_applies_and_archives_the_old_one(run_olist, db: Engine, tmp_path: Path) -> None:
    pending = olist_row(ORDERS, order_status="shipped", order_delivered_customer_date="")
    write_olist(tmp_path, olist_orders_dataset=[pending])
    run_olist(tmp_path)

    write_rows(tmp_path / "olist_orders_dataset_update.csv", [olist_row(ORDERS)])
    result = run_olist(tmp_path)

    assert result.metrics.table("orders").load.updated == 1
    assert scalar(db, "SELECT order_status FROM olist.orders") == "delivered"
    assert scalar(db, "SELECT previous_record->>'order_status' FROM ops.record_history") == "shipped"


def test_shuffled_file_gives_the_same_totals_as_the_ordered_one(run_hotel, db: Engine, settings: Settings) -> None:
    generate_dataset(settings, "hotel_bookings", 300)
    root = settings.paths.generated_dir / "hotel_bookings" / "rows_300"
    answers = json.loads((settings.paths.expected_dir / "hotel_bookings" / "rows_300.json").read_text(encoding="utf-8"))

    result = run_hotel(root / "out_of_order")

    expected = answers["clean"]["hotel_bookings"]
    assert result.status is RunStatus.SUCCESS
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == expected["target_rows"]
    assert float(scalar(db, "SELECT sum(adr) FROM hotel.hotel_bookings")) == pytest.approx(expected["adr_total"], abs=0.01)
    assert scalar(db, "SELECT sum(total_guests) FROM hotel.hotel_bookings") == expected["guests_total"]


def test_generated_late_scenario_recovers_through_input_flags(run_olist, db: Engine, settings: Settings) -> None:
    generate_dataset(settings, "olist", 120)
    root = settings.paths.generated_dir / "olist" / "rows_120"
    answers = json.loads((settings.paths.expected_dir / "olist" / "rows_120.json").read_text(encoding="utf-8"))
    assert run_olist(root / "clean").succeeded

    step1 = tuple((root / "late" / "step1").glob("*.csv"))
    first = run_olist(input_files=step1)
    assert first.metrics.total("rejected") == 1

    step2 = tuple((root / "late" / "step2").glob("*.csv"))
    second = run_olist(input_files=step2)

    order_id = answers["late"]["order_id"]
    assert second.metrics.table("order_items").reprocessed == 1
    assert float(scalar(db, "SELECT sum(item_total) FROM olist.order_items WHERE order_id = :o", o=order_id)) == answers["late"]["item_total"]


def test_generated_out_of_order_scenario_keeps_the_newest_version(run_olist, db: Engine, settings: Settings) -> None:
    generate_dataset(settings, "olist", 120)
    root = settings.paths.generated_dir / "olist" / "rows_120"
    answers = json.loads((settings.paths.expected_dir / "olist" / "rows_120.json").read_text(encoding="utf-8"))
    assert run_olist(root / "clean").succeeded

    run_olist(input_files=tuple((root / "out_of_order" / "newer").glob("*.csv")))
    older = run_olist(input_files=tuple((root / "out_of_order" / "older").glob("*.csv")))

    assert older.metrics.table("orders").load.stale == 1
    assert scalar(db, "SELECT order_status FROM olist.orders WHERE order_id = :o", o=answers["out_of_order"]["order_id"]) == answers["out_of_order"]["final_status"]


def test_generated_schema_variants_behave_as_documented(run_hotel, settings: Settings) -> None:
    generate_dataset(settings, "hotel_bookings", 300)
    variants = settings.paths.generated_dir / "hotel_bookings" / "rows_300" / "variants"

    assert run_hotel(variants / "extra_column").status is RunStatus.SUCCESS
    assert run_hotel(variants / "missing_required_column").status is RunStatus.FAILED
