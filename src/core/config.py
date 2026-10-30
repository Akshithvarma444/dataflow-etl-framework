"""Load and validate framework settings from YAML and environment variables.

Precedence, highest first: real environment variables, the ``.env`` file, then
``config/settings.yaml``. Credentials come only from the first two.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import URL

from src.core.exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class _StrictModel(BaseModel):
    """Rejects unknown keys so a typo in YAML fails instead of being ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PathSettings(_StrictModel):
    raw_dir: Path
    samples_dir: Path
    generated_dir: Path
    expected_dir: Path
    processed_dir: Path
    rejected_dir: Path
    reports_dir: Path
    logs_dir: Path
    datasets_config_dir: Path


class PipelineSettings(_StrictModel):
    batch_size: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    retry_backoff_seconds: float = Field(ge=0)


class LoggingSettings(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class GeneratorSettings(_StrictModel):
    seed: int


class DatabaseSettings(_StrictModel):
    user: str
    password: SecretStr
    host: str
    port: int = Field(gt=0, lt=65536)
    name: str
    test_name: str

    def url(self, *, test: bool = False) -> URL:
        """Build the SQLAlchemy URL; its string form hides the password."""
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.test_name if test else self.name,
        )


class Settings(_StrictModel):
    paths: PathSettings
    pipeline: PipelineSettings
    logging: LoggingSettings
    generator: GeneratorSettings
    database: DatabaseSettings


def _read_yaml(path: Path) -> dict:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Settings file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Settings file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise ConfigurationError(f"Settings file must contain a mapping: {path}")
    return content


def _resolve_paths(paths: dict, root: Path) -> dict:
    """Anchor relative paths at the repository root, independent of the cwd."""
    return {key: root / value for key, value in paths.items()}


def _database_from_env(env: Mapping[str, str | None]) -> dict:
    required = {
        "user": "POSTGRES_USER",
        "password": "POSTGRES_PASSWORD",
        "name": "POSTGRES_DB",
        "test_name": "POSTGRES_TEST_DB",
        "host": "POSTGRES_HOST",
        "port": "POSTGRES_PORT",
    }
    missing = sorted(var for var in required.values() if not env.get(var))
    if missing:
        raise ConfigurationError(
            f"Missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and set them."
        )
    return {field: env[var] for field, var in required.items()}


def load_settings(
    settings_file: Path = DEFAULT_SETTINGS_FILE,
    env_file: Path = DEFAULT_ENV_FILE,
    root: Path = PROJECT_ROOT,
) -> Settings:
    """Return validated settings, or raise ``ConfigurationError`` saying what is wrong."""
    env = {**dotenv_values(env_file), **os.environ}
    raw = _read_yaml(settings_file)

    if "paths" in raw:
        raw["paths"] = _resolve_paths(raw["paths"], root)
    raw["database"] = _database_from_env(env)

    if env.get("DATAFLOW_BATCH_SIZE"):
        raw.setdefault("pipeline", {})["batch_size"] = env["DATAFLOW_BATCH_SIZE"]
    if env.get("DATAFLOW_LOG_LEVEL"):
        raw.setdefault("logging", {})["level"] = env["DATAFLOW_LOG_LEVEL"]

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        # Report locations and messages only: pydantic's default text echoes
        # input values, which here could include the database password.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"Invalid settings: {problems}") from None
