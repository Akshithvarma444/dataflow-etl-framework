"""Reading files: encodings, row numbering, malformed rows, and unreadable input."""

import json
from pathlib import Path

import pytest

from src.core.contract import SourceSpec
from src.core.exceptions import EmptySourceError, ExtractionError, SchemaDriftError, SourceNotFoundError
from src.extractors import create_extractor
from src.extractors.base import inspect_file

CSV = SourceSpec(pattern="*.csv", format="csv")
JSON = SourceSpec(pattern="*.json", format="json")


def read_all(path: Path, source: SourceSpec = CSV, batch_size: int = 100):
    batches = list(create_extractor(path, source).read_batches(batch_size))
    return batches


def test_csv_values_are_all_text_with_one_based_row_numbers(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id,amount\n1,007\n2,3.50\n", encoding="utf-8")

    frame = read_all(path)[0].frame

    assert frame["id"].tolist() == ["1", "2"]
    assert frame["amount"].tolist() == ["007", "3.50"]  # the leading zeros survive
    assert frame["source_row_number"].tolist() == [1, 2]


def test_row_numbers_count_records_when_quoted_fields_contain_newlines(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text('id,comment\n1,"two\nlines"\n2,ok\n', encoding="utf-8")

    frame = read_all(path)[0].frame

    assert frame["comment"].tolist() == ["two\nlines", "ok"]
    assert frame["source_row_number"].tolist() == [1, 2]


def test_malformed_row_is_captured_and_later_rows_still_read(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id,amount\n1,10\n2\n3,30,extra\n4,40\n", encoding="utf-8")

    batch = read_all(path)[0]

    assert batch.frame["id"].tolist() == ["1", "4"]
    assert [row.source_row_number for row in batch.malformed] == [2, 3]
    assert batch.malformed[0].raw == ["2"]
    assert "expected 2 fields, found 3" in batch.malformed[1].reason


def test_batches_split_at_batch_size_and_keep_numbering(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id\n" + "".join(f"{i}\n" for i in range(5)), encoding="utf-8")

    batches = read_all(path, batch_size=2)

    assert [len(batch.frame) for batch in batches] == [2, 2, 1]
    assert batches[2].frame["source_row_number"].tolist() == [5]


def test_blank_lines_are_not_records(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id,x\n1,a\n\n2,b\n", encoding="utf-8")

    batch = read_all(path)[0]

    assert batch.frame["source_row_number"].tolist() == [1, 2]
    assert batch.malformed == []


def test_byte_order_mark_is_not_part_of_the_first_column_name(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_bytes("﻿id,x\n1,a\n".encode("utf-8"))

    assert create_extractor(path, CSV).columns() == ["id", "x"]


def test_latin1_file_is_read_through_the_fallback_encoding(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_bytes("city\nsão paulo\n".encode("latin-1"))

    extractor = create_extractor(path, CSV)

    assert extractor.source.encoding == "latin-1"
    assert next(extractor.read_batches(10)).frame["city"].tolist() == ["são paulo"]


def test_unreadable_encoding_fails_before_any_row_is_read(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_bytes("city\nsão\n".encode("latin-1"))

    with pytest.raises(ExtractionError, match="not readable"):
        inspect_file(path, ("utf-8",))


def test_checksum_is_stable_and_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id\n1\n", encoding="utf-8")
    first = inspect_file(path, ("utf-8",)).checksum

    assert inspect_file(path, ("utf-8",)).checksum == first

    path.write_text("id\n2\n", encoding="utf-8")
    assert inspect_file(path, ("utf-8",)).checksum != first


def test_missing_file_raises_source_not_found(tmp_path: Path) -> None:
    with pytest.raises(SourceNotFoundError):
        create_extractor(tmp_path / "absent.csv", CSV)


def test_zero_byte_file_raises_empty_source(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_bytes(b"")

    with pytest.raises(EmptySourceError):
        create_extractor(path, CSV)


def test_repeated_column_name_is_schema_drift(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("id,id\n1,2\n", encoding="utf-8")

    with pytest.raises(SchemaDriftError, match="repeats columns"):
        create_extractor(path, CSV).columns()


def test_json_array_reads_with_nested_values_as_text(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(json.dumps([{"id": 1, "tags": ["x"], "ok": True}, {"id": 2, "tags": None, "ok": False}]), encoding="utf-8")

    frame = read_all(path, JSON)[0].frame

    assert frame["id"].tolist() == ["1", "2"]
    assert frame["tags"].tolist() == ['["x"]', None]
    assert frame["ok"].tolist() == ["1", "0"]


def test_newline_delimited_json_quarantines_only_the_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('{"id": 1}\n{not json}\n[1, 2]\n{"id": 4}\n', encoding="utf-8")

    batch = read_all(path, JSON)[0]

    assert batch.frame["id"].tolist() == ["1", "4"]
    assert batch.frame["source_row_number"].tolist() == [1, 4]
    assert [row.source_row_number for row in batch.malformed] == [2, 3]
    assert batch.malformed[0].line_number == 2


def test_invalid_json_array_fails_the_file(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('[{"id": 1},', encoding="utf-8")

    with pytest.raises(ExtractionError, match="not valid JSON"):
        read_all(path, JSON)
