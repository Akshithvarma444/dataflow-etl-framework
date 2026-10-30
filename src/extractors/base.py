"""Common extraction behavior: file checks, checksums, and encoding detection."""

import codecs
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.core.exceptions import EmptySourceError, ExtractionError, SourceNotFoundError

READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SourceFile:
    """Identity of one input file. ``name`` is stable across machines, the path is not."""

    path: Path
    name: str
    checksum: str
    size_bytes: int
    encoding: str


@dataclass(frozen=True)
class MalformedRow:
    """A row that could not be parsed into the file's columns."""

    source_row_number: int
    line_number: int | None
    raw: object
    reason: str


@dataclass
class RawBatch:
    """Parsed rows as text, plus rows that could not be parsed.

    ``frame`` has one object column per source column and a ``source_row_number``.
    """

    frame: pd.DataFrame
    malformed: list[MalformedRow] = field(default_factory=list)


def inspect_file(path: Path, encodings: tuple[str, ...]) -> SourceFile:
    """Check a file exists and has content; return its checksum and encoding.

    The whole file is read here, so a truncated or undecodable file is found before any
    row has been processed.
    """
    if not path.is_file():
        raise SourceNotFoundError(f"Source file not found: {path}")
    size = path.stat().st_size
    if size == 0:
        raise EmptySourceError(f"Source file is empty: {path.name}")

    digest = hashlib.sha256()
    decoders = {name: codecs.getincrementaldecoder(name)() for name in encodings}
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK_BYTES):
            digest.update(chunk)
            for name, decoder in list(decoders.items()):
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    del decoders[name]
    if not decoders:
        raise ExtractionError(f"{path.name} is not readable as any of: {', '.join(encodings)}")

    encoding = next(name for name in encodings if name in decoders)
    return SourceFile(path, path.name, digest.hexdigest(), size, encoding)


class BaseExtractor(ABC):
    """Reads one source file into batches of text rows."""

    def __init__(self, source: SourceFile) -> None:
        self.source = source

    @abstractmethod
    def columns(self) -> list[str]:
        """Return the file's column names, for schema checks before reading rows."""

    @abstractmethod
    def read_batches(self, batch_size: int) -> Iterator[RawBatch]:
        """Yield rows in file order. Row numbers count records from 1, excluding any header."""
