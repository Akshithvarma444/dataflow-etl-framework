"""Settings loading: precedence, secret handling, and failure messages."""

from pathlib import Path

import pytest

from src.core.config import PROJECT_ROOT, load_settings
from src.core.exceptions import ConfigurationError

pytestmark = pytest.mark.usefixtures("clean_env")


def test_committed_settings_file_loads_with_expected_values(env_file: Path) -> None:
    settings = load_settings(env_file=env_file)

    assert settings.pipeline.batch_size == 50000
    assert settings.generator.seed == 42
    assert settings.logging.level == "INFO"


def test_relative_paths_resolve_from_repository_root_not_cwd(
    env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings(env_file=env_file)

    assert settings.paths.raw_dir == PROJECT_ROOT / "data" / "raw"


def test_real_environment_overrides_env_file(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    database = load_settings(env_file=env_file).database

    assert (database.host, database.port) == ("db", 5432)
    assert database.user == "test_user"  # untouched values still come from the file


def test_environment_overrides_yaml_pipeline_settings(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATAFLOW_BATCH_SIZE", "123")
    monkeypatch.setenv("DATAFLOW_LOG_LEVEL", "DEBUG")

    settings = load_settings(env_file=env_file)

    assert settings.pipeline.batch_size == 123
    assert settings.logging.level == "DEBUG"


def test_missing_credentials_error_names_each_variable(tmp_path: Path) -> None:
    empty_env = tmp_path / ".env"
    empty_env.write_text("POSTGRES_USER=only_this\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=empty_env)

    message = str(excinfo.value)
    assert "POSTGRES_PASSWORD" in message and "POSTGRES_DB" in message
    assert "POSTGRES_USER" not in message


def test_password_never_appears_in_repr_or_url_string(env_file: Path) -> None:
    database = load_settings(env_file=env_file).database

    assert "from_env_file" not in repr(database)
    assert "from_env_file" not in str(database.url())
    assert database.url().password == "from_env_file"


def test_password_with_url_special_characters_survives_round_trip(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1")

    url = load_settings(env_file=env_file).database.url()

    assert url.password == "p@ss:w/rd#1"
    assert url.host == "localhost"
    assert url.database == "main_db"


def test_test_url_targets_the_dedicated_test_database(env_file: Path) -> None:
    database = load_settings(env_file=env_file).database

    assert database.url(test=True).database == "test_db"
    assert database.url().database == "main_db"


def test_invalid_port_is_rejected_without_echoing_the_password(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "not_a_port")

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=env_file)

    assert "database.port" in str(excinfo.value)
    assert "from_env_file" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("yaml_text", "expected_fragment"),
    [
        ("pipeline: [unclosed", "not valid YAML"),
        ("- just\n- a list\n", "must contain a mapping"),
        (
            "paths: {}\nlogging: {level: INFO}\ngenerator: {seed: 1}\n"
            "pipeline: {batch_size: 0, max_retries: 1, retry_backoff_seconds: 1}\n",
            "pipeline.batch_size",
        ),
        (
            "paths: {}\nlogging: {level: INFO}\ngenerator: {seed: 1}\n"
            "pipeline: {batch_size: 1, max_retries: 1, retry_backoff_seconds: 1, batchsize: 9}\n",
            "pipeline.batchsize",
        ),
    ],
    ids=["malformed_yaml", "not_a_mapping", "batch_size_zero", "misspelled_key"],
)
def test_bad_settings_file_raises_a_specific_error(
    env_file: Path, tmp_path: Path, yaml_text: str, expected_fragment: str
) -> None:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=expected_fragment):
        load_settings(settings_file=settings_file, env_file=env_file)


def test_missing_settings_file_raises_configuration_error(
    env_file: Path, tmp_path: Path
) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_settings(settings_file=tmp_path / "absent.yaml", env_file=env_file)
