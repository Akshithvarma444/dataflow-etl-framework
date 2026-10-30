"""The generator: reproducibility, real schemas, and separation of answers from inputs."""

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src.core.config import Settings
from src.core.contract import DatasetContract
from src.core.exceptions import ConfigurationError
from src.generators.data_generator import generate_dataset


def digest(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*.csv"))
    }


@pytest.mark.parametrize("dataset", ["hotel_bookings", "olist"])
def test_same_seed_produces_identical_files(settings: Settings, tmp_path: Path, dataset: str) -> None:
    root = settings.paths.samples_dir / dataset / "rows_50"

    generate_dataset(settings, dataset, 50, seed=42)
    first = digest(root)
    generate_dataset(settings, dataset, 50, seed=42)

    assert digest(root) == first


@pytest.mark.parametrize("dataset", ["hotel_bookings", "olist"])
def test_different_seed_produces_different_files(settings: Settings, dataset: str) -> None:
    root = settings.paths.samples_dir / dataset / "rows_60"

    generate_dataset(settings, dataset, 60, seed=1)
    first = digest(root)
    generate_dataset(settings, dataset, 60, seed=2)

    assert digest(root) != first


def test_hotel_files_use_exactly_the_contract_columns(settings: Settings, hotel_contract: DatasetContract) -> None:
    generate_dataset(settings, "hotel_bookings", 50)
    frame = pd.read_csv(settings.paths.samples_dir / "hotel_bookings" / "rows_50" / "clean" / "hotel_bookings.csv", nrows=1)

    assert list(frame.columns) == hotel_contract.table("hotel_bookings").source_column_names


def test_olist_files_use_exactly_the_contract_columns(settings: Settings, olist_contract: DatasetContract) -> None:
    generate_dataset(settings, "olist", 50)
    clean = settings.paths.samples_dir / "olist" / "rows_50" / "clean"
    for table in olist_contract.tables:
        files = list(clean.glob(table.source.pattern))
        assert len(files) == 1, table.name
        assert list(pd.read_csv(files[0], nrows=0).columns) == table.source_column_names


def test_answers_are_kept_outside_the_input_directories(settings: Settings) -> None:
    generate_dataset(settings, "hotel_bookings", 50)
    inputs = settings.paths.samples_dir / "hotel_bookings" / "rows_50"

    names = {path.name for path in inputs.rglob("*") if path.is_file()}
    assert "rows_50.json" not in names
    assert all(not name.startswith("expected") for name in names)

    answers = json.loads((settings.paths.expected_dir / "hotel_bookings" / "rows_50.json").read_text(encoding="utf-8"))
    assert answers["dirty"]["hotel_bookings"]["rejected"] > 0
    assert answers["defect_rows"]["hotel_bookings"]


def test_input_files_contain_no_labels(settings: Settings) -> None:
    generate_dataset(settings, "hotel_bookings", 50)
    dirty = settings.paths.samples_dir / "hotel_bookings" / "rows_50" / "dirty" / "hotel_bookings.csv"

    assert "expected_rule" not in dirty.read_text(encoding="utf-8")


def test_every_directory_is_marked_synthetic(settings: Settings) -> None:
    generate_dataset(settings, "olist", 50)
    root = settings.paths.samples_dir / "olist" / "rows_50"

    for csv_file in root.rglob("*.csv"):
        manifest = json.loads((csv_file.parent / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["origin"] == "synthetic"
        assert manifest["seed"] == 42


def test_requested_size_is_exact_for_the_hotel_baseline(settings: Settings) -> None:
    generate_dataset(settings, "hotel_bookings", 250)
    path = settings.paths.generated_dir / "hotel_bookings" / "rows_250" / "clean" / "hotel_bookings.csv"

    assert len(pd.read_csv(path)) == 250


def test_rows_must_be_positive(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="positive"):
        generate_dataset(settings, "olist", 0)
