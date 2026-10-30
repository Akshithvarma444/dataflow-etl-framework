"""Shared pytest fixtures."""

from pathlib import Path

import pytest

DB_ENV_VARS = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_TEST_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "DATAFLOW_BATCH_SIZE",
    "DATAFLOW_LOG_LEVEL",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's real settings so config tests see only what they set."""
    for var in DB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A complete ``.env`` file holding throwaway test values."""
    path = tmp_path / ".env"
    path.write_text(
        "POSTGRES_USER=test_user\n"
        "POSTGRES_PASSWORD=from_env_file\n"
        "POSTGRES_DB=main_db\n"
        "POSTGRES_TEST_DB=test_db\n"
        "POSTGRES_HOST=localhost\n"
        "POSTGRES_PORT=5434\n",
        encoding="utf-8",
    )
    return path
