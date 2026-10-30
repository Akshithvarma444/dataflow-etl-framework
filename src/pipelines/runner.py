"""Resolves dataset names to contracts and runs them, one failure not stopping the next."""

from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.core.contract import available_datasets, load_contract
from src.core.exceptions import ConfigurationError
from src.database.connection import create_db_engine, ensure_database
from src.pipelines.pipeline import Pipeline, RunOptions, RunResult

ALL = "all"


def resolve_datasets(settings: Settings, name: str) -> list[str]:
    known = available_datasets(settings.paths.datasets_config_dir)
    if name == ALL:
        return known
    if name not in known:
        raise ConfigurationError(f"Unknown dataset {name!r}. Available: {', '.join(known)}")
    return [name]


def run_datasets(settings: Settings, names: list[str], options: RunOptions) -> list[RunResult]:
    """Run each dataset in turn. A database that cannot be reached fails each run cleanly."""
    try:
        ensure_database(settings.database, settings.database.name)
    except SQLAlchemyError:
        pass  # Pipeline.run records the connection failure with its reason.

    engine = create_db_engine(settings.database)
    try:
        results = []
        for name in names:
            contract = load_contract(settings.paths.datasets_config_dir / f"{name}.yaml")
            results.append(Pipeline(settings, contract, engine, options).run())
        return results
    finally:
        engine.dispose()
