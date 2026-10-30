"""Reusable source readers."""

from pathlib import Path

from src.core.contract import SourceSpec
from src.extractors.base import BaseExtractor, inspect_file
from src.extractors.csv_extractor import CsvExtractor
from src.extractors.json_extractor import JsonExtractor

_EXTRACTORS: dict[str, type[BaseExtractor]] = {"csv": CsvExtractor, "json": JsonExtractor}


def create_extractor(path: Path, source: SourceSpec) -> BaseExtractor:
    """Inspect a file and return the extractor for its configured format."""
    return _EXTRACTORS[source.format](inspect_file(path, source.encodings))
