"""JSON extraction: a top-level array of objects, or newline-delimited objects."""

import json
from collections.abc import Iterator

from src.core.constants import MALFORMED_ROW
from src.core.exceptions import EmptySourceError, ExtractionError
from src.extractors.base import BaseExtractor, MalformedRow, RawBatch
from src.extractors.csv_extractor import make_batch


def _as_text(value: object) -> str | None:
    """Match the CSV extractor: values are text, and nested values stay as JSON text."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class JsonExtractor(BaseExtractor):
    def _records(self) -> Iterator[tuple[int, object, str | None]]:
        """Yield (line number, parsed value, parse error) for every record in the file."""
        text = self.source.path.read_text(encoding=self.source.encoding)
        if not text.strip():
            raise EmptySourceError(f"Source file is empty: {self.source.name}")
        if text.lstrip().startswith("["):
            # An array is one document, so a syntax error leaves no rows to salvage.
            try:
                document = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ExtractionError(f"{self.source.name} is not valid JSON: {exc}") from exc
            for record in document:
                yield 0, record, None
            return
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line), None
            except json.JSONDecodeError as exc:
                yield line_number, line, str(exc)

    def columns(self) -> list[str]:
        names: dict[str, None] = {}
        for _, record, error in self._records():
            if error is None and isinstance(record, dict):
                names.update(dict.fromkeys(record))
        return list(names)

    def read_batches(self, batch_size: int) -> Iterator[RawBatch]:
        columns = self.columns()
        rows: list[list[str | None]] = []
        numbers: list[int] = []
        malformed: list[MalformedRow] = []
        for number, (line_number, record, error) in enumerate(self._records(), start=1):
            if error is None and isinstance(record, dict):
                rows.append([_as_text(record.get(column)) for column in columns])
                numbers.append(number)
            else:
                reason = error or f"expected an object, found {type(record).__name__}"
                malformed.append(MalformedRow(number, line_number or None, record, f"{MALFORMED_ROW}: {reason}"))
            if len(rows) + len(malformed) >= batch_size:
                yield make_batch(columns, rows, numbers, malformed)
                rows, numbers, malformed = [], [], []
        if rows or malformed:
            yield make_batch(columns, rows, numbers, malformed)
