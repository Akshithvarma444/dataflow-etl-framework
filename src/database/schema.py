"""Builds target tables from a contract, so a new dataset needs no table code."""

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from src.core.constants import (
    DQ_WARNINGS,
    OPS_SCHEMA,
    ROW_HASH,
    RuleSeverity,
    SOURCE_FILE,
    SOURCE_ROW,
)
from src.core.contract import ColumnSpec, DatasetContract, TableContract
from src.core.exceptions import SchemaDriftError
from src.database.models import OpsBase

_TYPES = {
    "text": Text,
    "integer": Integer,
    "decimal": lambda: Numeric(14, 2),
    "float": Float,
    "timestamp": DateTime,  # the sources state no timezone, so none is invented
    "date": Date,
}


def _column(spec: ColumnSpec) -> Column:
    return Column(spec.name, _TYPES[spec.type](), nullable=not spec.required, comment=spec.description or None)


def _audit_columns() -> list[Column]:
    return [
        Column(SOURCE_FILE, Text, nullable=False),
        Column(SOURCE_ROW, BigInteger, nullable=False),
        Column("first_run_id", UUID(as_uuid=True), nullable=False),
        Column("last_run_id", UUID(as_uuid=True), nullable=False),
        Column(ROW_HASH, Text, nullable=False),
        Column(DQ_WARNINGS, Text),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ]


def build_metadata(contract: DatasetContract) -> MetaData:
    """Describe every table in the dataset. Only error-level references become foreign keys."""
    metadata = MetaData(schema=contract.db_schema)
    for table in contract.tables:
        foreign_keys = [
            ForeignKeyConstraint([r.column], [f"{contract.db_schema}.{r.parent_table}.{r.parent_column}"])
            for r in table.references
            if RuleSeverity(r.on_missing) is RuleSeverity.ERROR
        ]
        built = Table(table.name, metadata, *(_column(c) for c in table.all_columns), *_audit_columns(),
                      PrimaryKeyConstraint(*table.primary_key), *foreign_keys)
        for index in table.indexes:
            Index(f"ix_{table.name}_{'_'.join(index)}", *(built.c[name] for name in index))
    for derived in contract.derived_tables:
        Table(derived.name, metadata, *(_column(c) for c in derived.columns),
              PrimaryKeyConstraint(*derived.primary_key))
    return metadata


def ensure_ops_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}"))
        OpsBase.metadata.create_all(conn)


def ensure_target_schema(conn: Connection, contract: DatasetContract) -> None:
    """Create missing tables and refuse to write to tables that no longer match the contract."""
    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {contract.db_schema}"))
    metadata = build_metadata(contract)
    metadata.create_all(conn)
    inspector = inspect(conn)
    for table in metadata.tables.values():
        existing = {column["name"] for column in inspector.get_columns(table.name, schema=contract.db_schema)}
        expected = {column.name for column in table.columns}
        if existing != expected:
            raise SchemaDriftError(
                f"Target table {table.fullname} does not match the contract "
                f"(differs on {sorted(existing ^ expected)}). Run with --full-refresh to rebuild it."
            )


def drop_target_schema(conn: Connection, contract: DatasetContract) -> None:
    conn.execute(text(f"DROP SCHEMA IF EXISTS {contract.db_schema} CASCADE"))
