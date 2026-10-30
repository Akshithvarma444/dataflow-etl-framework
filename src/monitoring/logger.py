"""Structured logging: JSON lines to a file, readable text to the console."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

FIELDS = ("run_id", "dataset", "stage", "table", "source_file", "input_count", "valid_count",
          "rejected_count", "inserted_count", "updated_count", "skipped_count", "duration_seconds",
          "status", "failure_reason")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({name: getattr(record, name) for name in FIELDS if hasattr(record, name)})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str, logs_dir: Path) -> None:
    """Set up the root logger. Safe to call repeatedly: it replaces earlier handlers."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    file_handler = logging.FileHandler(logs_dir / "dataflow.log", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


class _RunAdapter(logging.LoggerAdapter):
    """Keeps a call's own ``extra`` fields, which the standard adapter discards on Python 3.12."""

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs


def run_logger(name: str, run_id: object, dataset: str) -> logging.LoggerAdapter:
    """A logger that stamps every record with the run and dataset."""
    return _RunAdapter(logging.getLogger(name), {"run_id": str(run_id), "dataset": dataset})
