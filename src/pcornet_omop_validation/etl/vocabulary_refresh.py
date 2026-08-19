from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists
from .vocabulary import VOCABULARY_TABLES, _count_rows_and_hash, load_vocabulary


# Child/detail tables first. We refuse to proceed if SQL Server reports any
# foreign-key constraints involving these tables, so TRUNCATE remains explicit
# and fast for the large Athena tables.
TRUNCATE_ORDER = tuple(table for _, table in reversed(VOCABULARY_TABLES))


def _validate_bundle(config) -> list[dict[str, object]]:
    missing = []
    bundle = []
    for filename, table in VOCABULARY_TABLES:
        path = config.vocabulary_dir / filename
        if not path.exists():
            missing.append(filename)
            continue
        rows, sha256 = _count_rows_and_hash(path)
        if rows <= 0:
            raise RuntimeError(f"Vocabulary file has no data rows: {path}")
        bundle.append(
            {
                "file": str(path),
                "table": table,
                "source_rows": rows,
                "sha256": sha256,
            }
        )
    if missing:
        raise RuntimeError(
            "Vocabulary refresh requires the complete Athena table set; missing: "
            + ", ".join(missing)
        )
    return bundle


def _foreign_keys_involving(connection, schema: str) -> list[str]:
    names = [table for _, table in VOCABULARY_TABLES]
    placeholders = ", ".join(f":t{i}" for i in range(len(names)))
    params = {f"t{i}": name for i, name in enumerate(names)}
    params["schema"] = schema
    rows = connection.execute(
        text(
            f"""
            SELECT fk.name,
                   OBJECT_SCHEMA_NAME(fk.parent_object_id) AS parent_schema,
                   OBJECT_NAME(fk.parent_object_id) AS parent_table,
                   OBJECT_SCHEMA_NAME(fk.referenced_object_id) AS referenced_schema,
                   OBJECT_NAME(fk.referenced_object_id) AS referenced_table
            FROM sys.foreign_keys fk
            WHERE (
                    OBJECT_SCHEMA_NAME(fk.parent_object_id) = :schema
                AND OBJECT_NAME(fk.parent_object_id) IN ({placeholders})
                  )
               OR (
                    OBJECT_SCHEMA_NAME(fk.referenced_object_id) = :schema
                AND OBJECT_NAME(fk.referenced_object_id) IN ({placeholders})
                  )
            ORDER BY fk.name
            """
        ),
        params,
    ).fetchall()
    return [
        f"{r[0]}: [{r[1]}].[{r[2]}] -> [{r[3]}].[{r[4]}]" for r in rows
    ]


def refresh_vocabulary(config_path: str, confirm_database: str) -> int:
    config = load_etl_config(config_path)
    database = str(config.raw["sqlserver"]["database"])
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))

    if confirm_database != database:
        raise RuntimeError(
            "Destructive confirmation mismatch: "
            f"configured database is {database!r}, but --confirm-database was {confirm_database!r}."
        )

    bundle = _validate_bundle(config)
    audit_path = config.audit_dir / "vocabulary_refresh.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    before_counts: dict[str, int] = {}
    try:
        with engine.connect() as connection:
            for _, table in VOCABULARY_TABLES:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(
                        f"Target vocabulary table [{schema}].[{table}] does not exist."
                    )
                before_counts[table] = int(
                    connection.execute(
                        text(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                    ).scalar_one()
                )

            foreign_keys = _foreign_keys_involving(connection, schema)
            if foreign_keys:
                raise RuntimeError(
                    "Vocabulary refresh refuses to TRUNCATE while foreign keys involve vocabulary tables:\n  "
                    + "\n  ".join(foreign_keys)
                )

            started_payload = {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "vocabulary_refresh",
                "status": "refresh_started",
                "database": database,
                "schema": schema,
                "vocabulary_directory": str(config.vocabulary_dir),
                "confirmation_database": confirm_database,
                "before_counts": before_counts,
                "bundle": bundle,
                "note": (
                    "This operation replaces only OMOP vocabulary tables in the configured isolated target. "
                    "Clinical/source staging tables are not truncated by this command."
                ),
            }
            audit_path.write_text(
                json.dumps(started_payload, indent=2, sort_keys=True), encoding="utf-8"
            )

            print(f"Refreshing vocabulary tables in {database}.{schema}", flush=True)
            for table in TRUNCATE_ORDER:
                print(f"  TRUNCATE [{schema}].[{table}]", flush=True)
                connection.exec_driver_sql(f"TRUNCATE TABLE [{schema}].[{table}]")
            connection.commit()
    finally:
        engine.dispose()

    # Reuse the normal audited loader. It is resumable if a later table load fails.
    load_result = load_vocabulary(config)

    after_counts = {item.table.split(".", 1)[1]: item.target_rows for item in load_result.tables}
    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "vocabulary_refresh",
        "status": "refreshed_and_reconciled",
        "database": database,
        "schema": schema,
        "vocabulary_directory": str(config.vocabulary_dir),
        "confirmation_database": confirm_database,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "bundle": bundle,
        "vocabulary_load_audit": str(load_result.audit_path),
        "note": (
            "Vocabulary tables were explicitly replaced from one complete Athena bundle and row-count reconciled. "
            "Downstream concept-mapping audits should be rerun before further fact-table ETL."
        ),
    }
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print("Vocabulary refresh complete and reconciled.", flush=True)
    for item in load_result.tables:
        print(
            f"  {item.table}: {item.target_rows:,} rows sha256={item.sha256}",
            flush=True,
        )
    print(f"Refresh audit: {audit_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly replace and reconcile OMOP vocabulary tables from the configured Athena bundle."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--confirm-database",
        required=True,
        help="Must exactly match sqlserver.database in the config (for example OMOP_VALIDATED).",
    )
    args = parser.parse_args(argv)
    try:
        return refresh_vocabulary(args.config, args.confirm_database)
    except Exception as exc:
        print(f"ERROR: vocabulary refresh failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
