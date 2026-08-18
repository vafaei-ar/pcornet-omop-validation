from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import EtlConfig
from .database import connect, ensure_database, execute_script, table_exists
from .dependencies import acquire_common_data_model


@dataclass(frozen=True)
class SchemaResult:
    database_created: bool
    ddl_path: Path
    batches_executed: int
    already_present: bool


def _find_ddl(source_root: Path) -> Path:
    matches = list(source_root.rglob("OMOPCDM_sql_server_5.4_ddl.sql"))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected exactly one SQL Server OMOP 5.4 DDL under {source_root}; found {len(matches)}"
        )
    return matches[0]


def apply_omop_schema(config: EtlConfig) -> SchemaResult:
    """Create the target database if needed and apply the pinned OHDSI OMOP DDL.

    This stage is intentionally non-destructive. If an OMOP person table already exists,
    the DDL is not re-applied. Destructive reset semantics will be implemented as a
    separate, explicit operation rather than hidden in schema creation.
    """
    asset = acquire_common_data_model(config)
    ddl_path = _find_ddl(asset.extracted_dir)
    created = ensure_database(config)

    schema_name = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    with connect(config) as connection:
        if table_exists(connection, schema_name, "person"):
            return SchemaResult(
                database_created=created,
                ddl_path=ddl_path,
                batches_executed=0,
                already_present=True,
            )

        sql_text = ddl_path.read_text(encoding="utf-8-sig")
        # OHDSI release DDLs use @cdmDatabaseSchema as a render-time placeholder.
        sql_text = sql_text.replace("@cdmDatabaseSchema", schema_name)
        batches = execute_script(connection, sql_text)

    return SchemaResult(
        database_created=created,
        ddl_path=ddl_path,
        batches_executed=batches,
        already_present=False,
    )
