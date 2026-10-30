"""The command line, including exit codes operators rely on."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine

import src.main as cli
from src.core.config import Settings
from tests.conftest import scalar
from tests.helpers import hotel_row, write_hotel


@pytest.fixture
def use_test_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)


def test_dataset_is_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_full_refresh_and_backfill_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--dataset", "olist", "--full-refresh", "--backfill"])


def test_invalid_run_id_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--dataset", "olist", "--run-id", "not-a-uuid"])


@pytest.mark.usefixtures("use_test_settings")
def test_unknown_dataset_exits_nonzero_and_names_the_options(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["--dataset", "nope"]) == 1
    assert "hotel_bookings" in capsys.readouterr().err


@pytest.mark.usefixtures("use_test_settings")
def test_generate_command_writes_files(settings: Settings, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["--generate", "--dataset", "hotel_bookings", "--rows", "30"]) == 0
    assert (settings.paths.samples_dir / "hotel_bookings" / "rows_30" / "clean" / "hotel_bookings.csv").is_file()


@pytest.mark.usefixtures("use_test_settings")
def test_run_command_loads_data_and_uses_the_supplied_run_id(db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    write_hotel(tmp_path, [hotel_row()])
    run_id = uuid.uuid4()

    code = cli.main(["--dataset", "hotel_bookings", "--source-dir", str(tmp_path), "--run-id", str(run_id), "--batch-size", "10"])

    assert code == 0
    assert str(run_id) in capsys.readouterr().out
    assert scalar(db, "SELECT status FROM ops.pipeline_runs WHERE run_id = :r", r=run_id) == "success"


@pytest.mark.usefixtures("use_test_settings")
def test_failed_run_exits_nonzero(db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = cli.main(["--dataset", "hotel_bookings", "--source-dir", str(tmp_path)])

    assert code == 1
    assert "No file matching" in capsys.readouterr().out


@pytest.mark.usefixtures("use_test_settings")
def test_input_file_matching_no_table_fails_rather_than_doing_nothing(db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    stray = tmp_path / "stray.csv"
    stray.write_text("a\n1\n", encoding="utf-8")

    assert cli.main(["--dataset", "hotel_bookings", "--input", str(stray)]) == 1
    assert "matches no table" in capsys.readouterr().out


@pytest.mark.usefixtures("use_test_settings")
def test_dry_run_flag_leaves_no_data(db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    write_hotel(tmp_path, [hotel_row()])

    assert cli.main(["--dataset", "hotel_bookings", "--source-dir", str(tmp_path), "--dry-run"]) == 0
    assert scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'hotel'") == 0
