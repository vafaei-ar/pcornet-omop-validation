from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


DERIVED_TARGET_TABLES = (
    "death",
    "specimen",
    "device_exposure",
    "drug_exposure",
    "observation",
    "measurement",
    "procedure_occurrence",
    "condition_occurrence",
    "visit_occurrence",
    "observation_period",
    "person",
)

SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}


def _identifier(value: object, label: str) -> str:
    result = str(value or "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {result!r}")
    return result


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one() or 0)


def _etl_tables(connection, schema: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            text(
                """
                SELECT TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = :schema
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND TABLE_NAME LIKE 'etl[_]%'
                ORDER BY TABLE_NAME
                """
            ),
            {"schema": schema},
        ).fetchall()
    ]


def _incoming_foreign_keys(connection, schema: str, table: str) -> int:
    return _scalar(
        connection,
        """
        SELECT COUNT_BIG(*)
        FROM sys.foreign_keys fk
        JOIN sys.tables referenced_table
          ON referenced_table.object_id = fk.referenced_object_id
        JOIN sys.schemas referenced_schema
          ON referenced_schema.schema_id = referenced_table.schema_id
        WHERE referenced_schema.name = :schema
          AND referenced_table.name = :table
        """,
        {"schema": schema, "table": table},
    )


def inspect_clean_reset(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    database = _identifier(sql_cfg.get("database"), "database")
    target_schema = _identifier(sql_cfg.get("target_schema", "dbo"), "target_schema")

    if database.lower() in SYSTEM_DATABASES:
        raise RuntimeError(f"Refusing clean reset against SQL Server system database {database!r}")

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            actual_database = str(con.execute(text("SELECT DB_NAME()" )).scalar_one())
            if actual_database != database:
                raise RuntimeError(
                    "Connected database does not match configured target: "
                    f"connected={actual_database!r}, configured={database!r}"
                )

            target_rows: dict[str, int | None] = {}
            incoming_fks: dict[str, int] = {}
            for table in DERIVED_TARGET_TABLES:
                if table_exists(con, target_schema, table):
                    target_rows[table] = _scalar(
                        con,
                        f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]",
                    )
                    incoming_fks[table] = _incoming_foreign_keys(con, target_schema, table)
                else:
                    target_rows[table] = None
                    incoming_fks[table] = 0

            etl_tables = _etl_tables(con, target_schema)
            etl_rows = {
                table: _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]",
                )
                for table in etl_tables
            }

            staging_count = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = :schema
                  AND TABLE_NAME LIKE 'PCORnet[_]%'
                """,
                {"schema": target_schema},
            )
            concept_rows = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[concept]")
                if table_exists(con, target_schema, "concept")
                else 0
            )

        return {
            "database": database,
            "target_schema": target_schema,
            "target_rows": target_rows,
            "incoming_foreign_keys": incoming_fks,
            "etl_tables": etl_tables,
            "etl_rows": etl_rows,
            "pcornet_staging_table_count": staging_count,
            "concept_rows": concept_rows,
            "preserves": {
                "omop_ddl": True,
                "vocabulary_tables": True,
                "pcornet_staging_tables": True,
            },
        }
    finally:
        engine.dispose()


def execute_clean_reset(
    config: EtlConfig,
    *,
    confirm_database: str,
    confirm_schema: str,
) -> dict[str, object]:
    inspection = inspect_clean_reset(config)
    database = str(inspection["database"])
    target_schema = str(inspection["target_schema"])

    if confirm_database != database:
        raise RuntimeError(
            "Database confirmation does not exactly match configured target; refusing reset"
        )
    if confirm_schema != target_schema:
        raise RuntimeError(
            "Schema confirmation does not exactly match configured target schema; refusing reset"
        )

    fk_blockers = {
        table: count
        for table, count in dict(inspection["incoming_foreign_keys"]).items()
        if int(count) > 0
    }
    if fk_blockers:
        raise RuntimeError(
            "Clean reset is blocked because target tables have incoming foreign keys. "
            "No constraints will be disabled automatically: " + repr(fk_blockers)
        )

    engine = make_engine(config)
    try:
        with engine.begin() as con:
            actual_database = str(con.execute(text("SELECT DB_NAME()" )).scalar_one())
            if actual_database != database:
                raise RuntimeError(
                    "Connected database changed before reset; refusing destructive operation"
                )

            # Drop only ETL-owned ledgers in the configured target schema. The
            # PCORnet staging and OMOP vocabulary tables are deliberately retained.
            for table in reversed(list(inspection["etl_tables"])):
                con.exec_driver_sql(f"DROP TABLE [{target_schema}].[{table}]")

            # Preserve the pinned OMOP DDL while clearing only tables produced by
            # the validated transformation. TRUNCATE is fast and deterministic.
            for table in DERIVED_TARGET_TABLES:
                if table_exists(con, target_schema, table):
                    con.exec_driver_sql(f"TRUNCATE TABLE [{target_schema}].[{table}]")

        post = inspect_clean_reset(config)
        remaining_nonzero = {
            table: rows
            for table, rows in dict(post["target_rows"]).items()
            if rows not in (0, None)
        }
        if remaining_nonzero or post["etl_tables"]:
            raise RuntimeError(
                "Post-reset verification failed: "
                f"nonzero_targets={remaining_nonzero}, etl_tables={post['etl_tables']}"
            )

        payload = {
            "stage": "clean_reset",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "reset_complete",
            "database": database,
            "target_schema": target_schema,
            "pre_reset": inspection,
            "post_reset": post,
            "destructive_scope": (
                "Only ETL-owned etl_* tables were dropped and validated derived OMOP "
                "target tables were truncated in the exact configured database/schema."
            ),
            "preserved": (
                "OMOP DDL, vocabulary tables, PCORnet staging tables, and all other "
                "databases were left untouched."
            ),
        }
        audit_path = config.audit_dir / "clean_reset.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or explicitly execute a guarded reset of validated ETL outputs."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-schema")
    args = parser.parse_args(argv)

    config = load_etl_config(args.config)
    inspection = inspect_clean_reset(config)

    if not args.execute:
        print("status: dry_run_only")
        print(f"database: {inspection['database']}")
        print(f"target_schema: {inspection['target_schema']}")
        print("destructive_actions_performed: False")
        print(f"target_rows: {inspection['target_rows']}")
        print(f"etl_tables: {inspection['etl_tables']}")
        print(f"incoming_foreign_keys: {inspection['incoming_foreign_keys']}")
        print(f"pcornet_staging_table_count: {inspection['pcornet_staging_table_count']}")
        print(f"concept_rows: {inspection['concept_rows']}")
        print("preserves: OMOP DDL, vocabulary, PCORnet staging, other databases")
        return 0

    if args.confirm_database is None or args.confirm_schema is None:
        parser.error("--execute requires --confirm-database and --confirm-schema")

    result = execute_clean_reset(
        config,
        confirm_database=args.confirm_database,
        confirm_schema=args.confirm_schema,
    )
    print(f"status: {result['status']}")
    print(f"database: {result['database']}")
    print(f"target_schema: {result['target_schema']}")
    print("destructive_actions_performed: True")
    print(f"post_reset_target_rows: {result['post_reset']['target_rows']}")
    print(f"post_reset_etl_tables: {result['post_reset']['etl_tables']}")
    print(f"pcornet_staging_table_count: {result['post_reset']['pcornet_staging_table_count']}")
    print(f"concept_rows: {result['post_reset']['concept_rows']}")
    print(f"Audit: {result['audit_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
