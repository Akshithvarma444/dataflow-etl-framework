"""Shared pytest fixtures.

Integration tests use only the dedicated POSTGRES_TEST_DB database. They are skipped, with
the reason shown, if it cannot be reached: start it with `docker compose up -d db`.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from src.core.config import Settings, load_settings
from src.core.contract import DatasetContract, load_contract
from src.database.connection import create_db_engine, ensure_database
from src.pipelines.pipeline import Pipeline, RunOptions, RunResult

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


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Real credentials, the test database, and every output path inside a temp directory."""
    base = load_settings()
    root = tmp_path_factory.mktemp("dataflow")
    paths = base.paths.model_copy(update={
        name: root / name
        for name in ("raw_dir", "samples_dir", "generated_dir", "expected_dir", "processed_dir",
                     "rejected_dir", "reports_dir", "logs_dir")
    })
    database = base.database.model_copy(update={"name": base.database.test_name})
    pipeline = base.pipeline.model_copy(update={"retry_backoff_seconds": 0.0})
    return base.model_copy(update={"paths": paths, "database": database, "pipeline": pipeline})


@pytest.fixture(scope="session")
def db_engine(settings: Settings) -> Iterator[Engine]:
    assert settings.database.name == settings.database.test_name
    try:
        ensure_database(settings.database, settings.database.name)
    except OperationalError as exc:
        message = f"Test database unreachable, run `docker compose up -d db`: {exc.orig}"
        if os.environ.get("DATAFLOW_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)
    engine = create_db_engine(settings.database)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Engine:
    """An empty test database."""
    with db_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS hotel, olist, ops CASCADE"))
    return db_engine


def _contract(settings: Settings, name: str) -> DatasetContract:
    return load_contract(settings.paths.datasets_config_dir / f"{name}.yaml")


@pytest.fixture(scope="session")
def hotel_contract(settings: Settings) -> DatasetContract:
    return _contract(settings, "hotel_bookings")


@pytest.fixture(scope="session")
def olist_contract(settings: Settings) -> DatasetContract:
    return _contract(settings, "olist")


@pytest.fixture
def run_hotel(settings: Settings, hotel_contract: DatasetContract, db: Engine) -> Callable[..., RunResult]:
    def run(source_dir: Path | None = None, **options) -> RunResult:
        return Pipeline(settings, hotel_contract, db, RunOptions(source_dir=source_dir, **options)).run()
    return run


@pytest.fixture
def run_olist(settings: Settings, olist_contract: DatasetContract, db: Engine) -> Callable[..., RunResult]:
    def run(source_dir: Path | None = None, **options) -> RunResult:
        return Pipeline(settings, olist_contract, db, RunOptions(source_dir=source_dir, **options)).run()
    return run


def scalar(engine: Engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows_of(engine: Engine, sql: str, **params) -> list:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().all()
