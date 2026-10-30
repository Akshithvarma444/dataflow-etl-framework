"""A source whose shape changes, and contracts that change over time."""

from pathlib import Path

from sqlalchemy import Engine, text

from src.core.config import Settings
from src.core.constants import RunStatus
from src.core.contract import DatasetContract
from src.pipelines.pipeline import Pipeline, RunOptions
from tests.conftest import scalar
from tests.helpers import HOTEL_ROW, hotel_row, write_hotel, write_rows


def test_missing_required_column_fails_the_run_and_publishes_nothing(run_hotel, db: Engine, tmp_path: Path) -> None:
    header = [name for name in HOTEL_ROW if name != "adr"]
    write_rows(tmp_path / "hotel_bookings.csv", [hotel_row()], header)

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.FAILED
    assert "missing required columns: ['adr']" in result.failure_reason
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 0
    assert scalar(db, "SELECT count(*) FROM ops.processed_files") == 0


def test_unexpected_column_is_ignored_reported_and_kept_out_of_the_target(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_rows(tmp_path / "hotel_bookings.csv", [{**hotel_row(), "loyalty_tier": "gold"}])

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    notes = result.metrics.table("hotel_bookings").schema_notes[0]
    assert notes["unexpected"] == ["loyalty_tier"]
    assert scalar(db, "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'hotel' AND column_name = 'loyalty_tier'") == 0


def test_rejected_row_keeps_the_unexpected_column_for_analysis(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_rows(tmp_path / "hotel_bookings.csv", [{**hotel_row(adr="-1"), "loyalty_tier": "gold"}])

    run_hotel(tmp_path)

    assert scalar(db, "SELECT original_record->>'loyalty_tier' FROM ops.rejected_records") == "gold"


def test_missing_optional_column_loads_as_null(run_hotel, db: Engine, tmp_path: Path) -> None:
    header = [name for name in HOTEL_ROW if name != "company"]
    write_rows(tmp_path / "hotel_bookings.csv", [hotel_row()], header)

    result = run_hotel(tmp_path)

    assert result.status is RunStatus.SUCCESS
    assert result.metrics.table("hotel_bookings").schema_notes[0]["missing_optional"] == ["company"]
    assert scalar(db, "SELECT company FROM hotel.hotel_bookings") is None


def test_reordered_columns_load_by_name_not_position(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_rows(tmp_path / "hotel_bookings.csv", [hotel_row(adr="77.00")], list(reversed(list(HOTEL_ROW))))

    assert run_hotel(tmp_path).status is RunStatus.SUCCESS
    assert float(scalar(db, "SELECT adr FROM hotel.hotel_bookings")) == 77.0


def test_strict_contract_fails_on_an_unexpected_column(settings: Settings, hotel_contract: DatasetContract, db: Engine, tmp_path: Path) -> None:
    table = hotel_contract.tables[0]
    strict_source = table.source.model_copy(update={"on_unexpected_column": "fail"})
    strict = hotel_contract.model_copy(update={"tables": (table.model_copy(update={"source": strict_source}),)})
    write_rows(tmp_path / "hotel_bookings.csv", [{**hotel_row(), "loyalty_tier": "gold"}])

    result = Pipeline(settings, strict, db, RunOptions(source_dir=tmp_path)).run()

    assert result.status is RunStatus.FAILED
    assert "unexpected columns" in result.failure_reason


def test_target_table_out_of_step_with_the_contract_fails_and_full_refresh_fixes_it(run_hotel, db: Engine, tmp_path: Path) -> None:
    write_hotel(tmp_path, [hotel_row()])
    run_hotel(tmp_path)
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE hotel.hotel_bookings DROP COLUMN agent"))

    blocked = run_hotel(tmp_path)
    assert blocked.status is RunStatus.FAILED
    assert "--full-refresh" in blocked.failure_reason

    fixed = run_hotel(tmp_path, full_refresh=True)
    assert fixed.status is RunStatus.SUCCESS
    assert scalar(db, "SELECT count(*) FROM hotel.hotel_bookings") == 1
