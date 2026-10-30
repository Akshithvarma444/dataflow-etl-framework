"""CSV extraction with csv.reader, so one bad row is quarantined rather than fatal."""

import csv
from collections.abc import Iterator

import pandas as pd

from src.core.constants import MALFORMED_ROW, SOURCE_ROW
from src.core.exceptions import EmptySourceError, ExtractionError, SchemaDriftError
from src.extractors.base import BaseExtractor, MalformedRow, RawBatch

MAX_FIELD_BYTES = 10 * 1024 * 1024
# A real record can contain newlines, but a stray unterminated quote swallows every line up to
# the next quote into one field that can look valid, losing those rows without any error.
MAX_LINES_PER_RECORD = 100


def make_batch(columns: list[str], rows: list[list[str]], numbers: list[int], malformed: list[MalformedRow]) -> RawBatch:
    # dtype=object keeps None as None and stops pandas inferring anything from the text.
    frame = pd.DataFrame(rows, columns=columns, dtype=object)
    frame[SOURCE_ROW] = pd.array(numbers, dtype="int64")
    return RawBatch(frame, malformed)


class CsvExtractor(BaseExtractor):
    def _open(self):
        return self.source.path.open("r", encoding=self.source.encoding, newline="")

    def columns(self) -> list[str]:
        csv.field_size_limit(MAX_FIELD_BYTES)
        with self._open() as handle:
            try:
                header = next(csv.reader(handle))
            except StopIteration:
                raise EmptySourceError(f"Source file has no header: {self.source.name}") from None
        names = [name.strip() for name in header]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            raise SchemaDriftError(f"{self.source.name} repeats columns: {duplicated}")
        return names

    def read_batches(self, batch_size: int) -> Iterator[RawBatch]:
        columns = self.columns()
        rows: list[list[str]] = []
        numbers: list[int] = []
        malformed: list[MalformedRow] = []
        number = 0
        with self._open() as handle:
            reader = csv.reader(handle)
            next(reader)
            try:
                previous_line = reader.line_num
                for fields in reader:
                    if reader.line_num - previous_line > MAX_LINES_PER_RECORD:
                        raise ExtractionError(
                            f"{self.source.name}: the record starting after line {previous_line} spans "
                            f"{reader.line_num - previous_line} lines; probably an unterminated quote."
                        )
                    previous_line = reader.line_num
                    if not fields:  # a blank line is not a record
                        continue
                    number += 1
                    if len(fields) == len(columns):
                        rows.append(fields)
                        numbers.append(number)
                    else:
                        malformed.append(
                            MalformedRow(
                                number,
                                reader.line_num,
                                fields,
                                f"{MALFORMED_ROW}: expected {len(columns)} fields, found {len(fields)}",
                            )
                        )
                    if len(rows) + len(malformed) >= batch_size:
                        yield make_batch(columns, rows, numbers, malformed)
                        rows, numbers, malformed = [], [], []
            except csv.Error as exc:
                # Usually an unterminated quote: the parser's position can no longer be trusted.
                raise ExtractionError(
                    f"{self.source.name} cannot be parsed after line {reader.line_num}: {exc}"
                ) from exc
        if rows or malformed:
            yield make_batch(columns, rows, numbers, malformed)
