"""Engine creation, database bootstrap, and the per-dataset run lock."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from src.core.config import DatabaseSettings
from src.core.exceptions import ConcurrentRunError, LoadError, TransientDatabaseError

CONNECT_TIMEOUT_SECONDS = 10


def create_db_engine(database: DatabaseSettings, *, name: str | None = None) -> Engine:
    """Create an engine, for ``name`` if given. The timeout stops a bad host hanging forever."""
    url = database.url()
    if name:
        url = url.set(database=name)
    return create_engine(url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS})


def ensure_database(database: DatabaseSettings, name: str) -> None:
    """Create a database if it does not exist. CREATE DATABASE cannot run in a transaction."""
    admin = create_db_engine(database, name="postgres").execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def classify_database_error(exc: DBAPIError) -> LoadError:
    """Only connection-level and lock failures may succeed on retry."""
    message = str(exc.orig) if exc.orig else str(exc)
    if isinstance(exc, OperationalError) or exc.connection_invalidated:
        return TransientDatabaseError(f"Database unavailable: {message}")
    return LoadError(f"Database write failed: {message}")


@contextmanager
def dataset_lock(engine: Engine, dataset: str) -> Iterator[None]:
    """Hold a session-level advisory lock for a dataset, so runs cannot overlap.

    The lock dies with its session, so a crashed run can never block later ones.
    """
    with engine.connect() as conn:
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:d))"), {"d": dataset}).scalar()
        conn.commit()
        if not acquired:
            raise ConcurrentRunError(f"Another run of {dataset} is in progress.")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:d))"), {"d": dataset})
            conn.commit()
