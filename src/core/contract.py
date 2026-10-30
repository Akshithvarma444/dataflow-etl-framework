"""The dataset contract: the YAML that tells the generic engine what a dataset looks like.

Adding a dataset means writing one of these, plus a transformer if it has derived fields.
"""

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.core.constants import DuplicatePolicy, LoadStrategy, RuleSeverity
from src.core.exceptions import ConfigurationError

ColumnType = Literal["text", "integer", "decimal", "float", "timestamp", "date"]
IDENTIFIER = r"^[a-z_][a-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnSpec(_Strict):
    # Identifiers end up in generated SQL, so the pattern is also a safety check.
    name: str = Field(pattern=IDENTIFIER)
    type: ColumnType
    required: bool = False
    description: str = ""
    null_tokens: tuple[str, ...] | None = None
    format: str | None = None


class SourceSpec(_Strict):
    pattern: str
    format: Literal["csv", "json"] = "csv"
    encodings: tuple[str, ...] = ("utf-8-sig", "latin-1")
    null_tokens: tuple[str, ...] = ("",)
    on_unexpected_column: Literal["warn", "fail"] = "warn"


class DuplicateSpec(_Strict):
    policy: DuplicatePolicy = DuplicatePolicy.REJECT
    fingerprint_column: str | None = None
    count_column: str | None = None
    occurrence_column: str | None = None

    @model_validator(mode="after")
    def _columns_match_policy(self) -> "DuplicateSpec":
        if self.policy is DuplicatePolicy.COLLAPSE and not (self.fingerprint_column and self.count_column):
            raise ValueError("collapse needs fingerprint_column and count_column")
        if self.policy is DuplicatePolicy.FLAG and not (self.fingerprint_column and self.occurrence_column):
            raise ValueError("flag needs fingerprint_column and occurrence_column")
        return self


class ReferenceSpec(_Strict):
    column: str
    parent_table: str
    parent_column: str
    # "error" also creates a database foreign key; "warning" is reported only.
    on_missing: RuleSeverity = RuleSeverity.ERROR


class RuleSpec(_Strict):
    name: str = Field(pattern=IDENTIFIER)
    check: Literal["range", "allowed_values", "regex", "compare", "custom"]
    severity: RuleSeverity = RuleSeverity.ERROR
    message: str = ""
    column: str | None = None
    min: float | None = None
    max: float | None = None
    min_exclusive: bool = False
    values: tuple[str, ...] | None = None
    pattern: str | None = None
    left: str | None = None
    op: Literal[">=", ">", "<=", "<", "=="] | None = None
    right: str | None = None

    @model_validator(mode="after")
    def _has_required_fields(self) -> "RuleSpec":
        needed = {
            "range": ("column",),
            "allowed_values": ("column", "values"),
            "regex": ("column", "pattern"),
            "compare": ("left", "op", "right"),
            "custom": (),
        }[self.check]
        missing = [field for field in needed if getattr(self, field) is None]
        if missing:
            raise ValueError(f"rule {self.name} ({self.check}) needs {missing}")
        return self


class LoadSpec(_Strict):
    strategy: LoadStrategy = LoadStrategy.UPSERT
    version_column: str | None = None


class AggregateSpec(_Strict):
    """A total compared between the source file and the target table.

    ``source_columns`` are summed from raw values independently of any transformation.
    An empty list means the number of source rows.
    """

    name: str = Field(pattern=IDENTIFIER)
    target_expression: str
    source_columns: tuple[str, ...] = ()


class TableContract(_Strict):
    name: str = Field(pattern=IDENTIFIER)
    description: str = ""
    grain: str = ""
    source: SourceSpec
    columns: tuple[ColumnSpec, ...]
    derived_columns: tuple[ColumnSpec, ...] = ()
    primary_key: tuple[str, ...]
    record_id_column: str | None = None
    duplicates: DuplicateSpec = DuplicateSpec()
    references: tuple[ReferenceSpec, ...] = ()
    rules: tuple[RuleSpec, ...] = ()
    load: LoadSpec = LoadSpec()
    aggregates: tuple[AggregateSpec, ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()

    @property
    def all_columns(self) -> tuple[ColumnSpec, ...]:
        return self.columns + self.derived_columns

    @property
    def source_column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    @model_validator(mode="after")
    def _columns_exist(self) -> "TableContract":
        names = {column.name for column in self.all_columns}
        referenced = list(self.primary_key)
        referenced += [reference.column for reference in self.references]
        referenced += [rule.column for rule in self.rules if rule.column]
        referenced += [rule.left for rule in self.rules if rule.left]
        referenced += [rule.right for rule in self.rules if rule.right]
        referenced += [column for index in self.indexes for column in index]
        if self.load.version_column:
            referenced.append(self.load.version_column)
        # source_file is an audit column that every target table has.
        unknown = sorted(set(referenced) - names - {"source_file"})
        if unknown:
            raise ValueError(f"table {self.name} refers to unknown columns: {unknown}")
        return self


class DerivedTableSpec(_Strict):
    """A table computed from published tables, not loaded from a file."""

    name: str = Field(pattern=IDENTIFIER)
    description: str = ""
    grain: str = ""
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    refresh_sql: tuple[str, ...]


class CheckSpec(_Strict):
    """A SQL statement returning a violation count, which must be zero to publish."""

    name: str = Field(pattern=IDENTIFIER)
    description: str = ""
    sql: str


class DatasetContract(_Strict):
    dataset: str = Field(pattern=IDENTIFIER)
    version: int = 1
    db_schema: str = Field(pattern=IDENTIFIER)
    description: str = ""
    default_source_dir: str
    transformer: str
    tables: tuple[TableContract, ...]
    derived_tables: tuple[DerivedTableSpec, ...] = ()
    checks: tuple[CheckSpec, ...] = ()

    @field_validator("tables")
    @classmethod
    def _parents_come_first(cls, tables: tuple[TableContract, ...]) -> tuple[TableContract, ...]:
        seen: set[str] = set()
        for table in tables:
            for reference in table.references:
                if reference.parent_table not in seen:
                    raise ValueError(
                        f"table {table.name} references {reference.parent_table}, "
                        "which must be listed before it"
                    )
            seen.add(table.name)
        return tables

    @property
    def content_hash(self) -> str:
        """Changes whenever anything that could affect validation or output changes."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    def table(self, name: str) -> TableContract:
        return next(table for table in self.tables if table.name == name)


def load_contract(path: Path) -> DatasetContract:
    """Read and validate one contract, or raise ``ConfigurationError`` saying what is wrong."""
    try:
        return DatasetContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Dataset contract not found: {path}") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid dataset contract {path.name}: {exc}") from exc


def available_datasets(config_dir: Path) -> list[str]:
    return sorted(path.stem for path in config_dir.glob("*.yaml"))
