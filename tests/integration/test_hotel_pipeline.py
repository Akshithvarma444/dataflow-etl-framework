"""End-to-end hotel runs, checked against the generator's independent answer key."""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from src.core.config import Settings
from src.core.constants import RunStatus
from src.generators.data_generator import generate_dataset
from tests.conftest import rows_of, scalar
from tests.helpers import hotel_row, write_hotel

SIZE = 2000


@pytest.fixture(scope="module")
def generated(settings: Settings) -> tuple[Path, dict]:
    generate_dataset(settings, "hotel_bookings", SIZE)
    root = settings.paths.generated_dir / "hotel_bookings" / f"rows_{SIZE}"
    answers = json.loads((settings.paths.expected_dir / "hotel_bookings" / f"rows_{SIZE}.json").read_text(encoding="utf-8"))
    return root, answers


def test_clean_data_matches_the_answer_key(run_hotel, db: Engine, generated) -> None:
    root, answers = generated
    expected = answers["clean"]["hotel_bookings"]

    result = run_hotel(root / "clean")

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.total("input") == expected["source_rows"]
    assert result.metrics.total("rejected") == 0
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == expected["target_rows"]
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings WHERE duplicate_occurrence > 1") == expected["flagged_duplicates"]
    assert float(scalar(db, "SELECT sum(adr) FROM hotel.hotel_bookings")) == pytest.approx(expected["adr_total"], abs=0.01)
    assert scalar(db, "SELECT sum(is_canceled) FROM hotel.hotel_bookings") == expected["cancellations"]
    assert scalar(db, "SELECT sum(total_guests) FROM hotel.hotel_bookings") == expected["guests_total"]
    assert scalar(db, "SELECT sum(total_nights) FROM hotel.hotel_bookings") == expected["nights_total"]
    assert float(scalar(db, "SELECT sum(estimated_booking_value) FROM hotel.hotel_bookings")) == pytest.approx(expected["estimated_value_total"], abs=0.01)
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings WHERE total_nights = 0") == expected["zero_night_stays"]


def test_dirty_data_rejects_exactly_the_labelled_rows_for_the_labelled_reasons(run_hotel, db: Engine, generated) -> None:
    root, answers = generated
    expected = answers["dirty"]["hotel_bookings"]

    result = run_hotel(root / "dirty")

    assert result.status is RunStatus.SUCCESS_WITH_REJECTS
    table = result.metrics.table("hotel_bookings")
    assert table.input == expected["source_rows"]
    assert table.rejected == expected["rejected"]
    assert table.malformed == expected["malformed"]
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == expected["target_rows"]
    assert float(scalar(db, "SELECT sum(adr) FROM hotel.hotel_bookings")) == pytest.approx(expected["adr_total"], abs=0.01)

    labels = {row["source_row_number"]: row["expected_rule"] for row in answers["defect_rows"]["hotel_bookings"]}
    stored = {row["source_row_number"]: row["rejection_reason"]
              for row in rows_of(db, "SELECT source_row_number, rejection_reason FROM ops.rejected_records")}
    assert set(stored) == set(labels)
    for row_number, rule in labels.items():
        assert rule in stored[row_number], f"row {row_number}"


def test_rejected_rows_trace_back_to_their_exact_source_line(run_hotel, db: Engine, generated) -> None:
    root, _ = generated
    source_lines = (root / "dirty" / "hotel_bookings.csv").read_text(encoding="utf-8").splitlines()

    result = run_hotel(root / "dirty")

    rows = rows_of(db, "SELECT * FROM ops.rejected_records WHERE rejection_reason LIKE '%negative_adr%' LIMIT 1")
    row = rows[0]
    assert row["run_id"] == result.run_id
    assert row["dataset_name"] == "hotel_bookings"
    assert row["source_file"] == "hotel_bookings.csv"
    assert row["rejected_at"] is not None
    # Generated rows have no embedded newlines, so row N is line N + 1 with the header.
    assert row["original_record"]["adr"] in source_lines[row["source_row_number"]]


def test_literal_null_tokens_become_real_nulls_per_column(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(children="NA", country="NULL", agent="NULL", company="NULL")])

    run_hotel(tmp_path)

    row = rows_of(db, "SELECT children, country, agent, company, total_guests FROM hotel.hotel_bookings")[0]
    assert (row["children"], row["country"], row["agent"], row["company"]) == (None, None, None, None)
    assert row["total_guests"] == 2


def test_namibia_country_code_is_not_mistaken_for_missing(run_hotel, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: NA means missing in `children` but a code can legitimately be NA."""
    write_hotel(tmp_path, [hotel_row(country="NA")])

    run_hotel(tmp_path)

    assert scalar(db, "SELECT country FROM hotel.hotel_bookings") == "NA"


def test_day_use_stay_is_loaded_and_flagged_not_rejected(run_hotel, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 680 of 715 real zero-night rows checked out, all at ADR 0."""
    write_hotel(tmp_path, [hotel_row(stays_in_weekend_nights="0", stays_in_week_nights="0", adr="0.00",
                                     reservation_status_date="2016-03-05")])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    row = rows_of(db, "SELECT total_nights, estimated_booking_value, dq_warnings FROM hotel.hotel_bookings")[0]
    assert row["total_nights"] == 0
    assert float(row["estimated_booking_value"]) == 0.0
    assert row["dq_warnings"] == "zero_night_stay"


def test_booking_with_no_guests_is_quarantined(run_hotel, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 180 real rows are bookings for nobody."""
    write_hotel(tmp_path, [hotel_row(adults="0", children="0", babies="0"), hotel_row()])

    run_hotel(tmp_path)

    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1
    assert scalar(db, "SELECT rejection_reason FROM ops.rejected_records") == "zero_guests"


def test_child_only_booking_is_accepted(run_hotel, db: Engine, tmp_path: Path) -> None:
    """Domain edge case: 223 real rows have no adults but do have guests."""
    write_hotel(tmp_path, [hotel_row(adults="0", children="2")])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert scalar(db, "SELECT total_guests FROM hotel.hotel_bookings") == 2


def test_extreme_rate_loads_with_a_warning(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row(adr="5400.00")])

    assert run_hotel(tmp_path).status is RunStatus.SUCCESS
    assert scalar(db, "SELECT dq_warnings FROM hotel.hotel_bookings") == "high_adr"


def test_report_and_metrics_are_written(run_hotel, db: Engine, generated) -> None:
    root, answers = generated

    result = run_hotel(root / "dirty")

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["totals"]["rejected"] == answers["dirty"]["hotel_bookings"]["rejected"]
    assert report["reconciliation"]["tables"]["hotel_bookings"]["target_rows_total"] == answers["dirty"]["hotel_bookings"]["target_rows"]
    assert all(check["status"] == "pass" for check in report["reconciliation"]["checks"])
    assert all(a["status"] == "match" for a in report["reconciliation"]["tables"]["hotel_bookings"]["aggregates"])
    assert report["performance"]["stage_seconds"]["loading"] > 0
    assert scalar(db, "SELECT metric_value FROM ops.pipeline_metrics WHERE run_id = :r AND table_name = 'hotel_bookings' AND metric_name = 'rejected'",
                  r=result.run_id) == answers["dirty"]["hotel_bookings"]["rejected"]


def test_rejected_csv_and_sample_outputs_are_written(run_hotel, settings: Settings, generated) -> None:
    root, _ = generated

    result = run_hotel(root / "dirty")

    assert (settings.paths.rejected_dir / "hotel_bookings" / f"rejected_{result.run_id}.csv").is_file()
    assert (settings.paths.processed_dir / "hotel_bookings" / "hotel_bookings_sample.csv").is_file()


def test_synthetic_runs_are_labelled_in_run_history(run_hotel, db: Engine, generated, tmp_path: Path) -> None:
    root, _ = generated

    synthetic = run_hotel(root / "clean")
    assert scalar(db, "SELECT data_origin FROM ops.pipeline_runs WHERE run_id = :r", r=synthetic.run_id) == "synthetic"

    write_hotel(tmp_path, [hotel_row()])
    supplied = run_hotel(tmp_path)
    assert scalar(db, "SELECT data_origin FROM ops.pipeline_runs WHERE run_id = :r", r=supplied.run_id) == "source"
