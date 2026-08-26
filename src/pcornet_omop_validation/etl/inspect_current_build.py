from __future__ import annotations

import argparse
import re

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


CORE_TABLES = (
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def inspect_current_build(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            target_rows: dict[str, int | None] = {}
            for table in CORE_TABLES:
                if table_exists(con, target_schema, table):
                    target_rows[table] = _scalar(
                        con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]"
                    )
                else:
                    target_rows[table] = None

            etl_tables = [
                str(row[0])
                for row in con.execute(
                    text(
                        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME LIKE 'etl[_]%' "
                        "ORDER BY TABLE_NAME"
                    ),
                    {"schema": target_schema},
                ).fetchall()
            ]

            staging_tables = [
                str(row[0])
                for row in con.execute(
                    text(
                        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME LIKE 'PCORnet[_]%' "
                        "ORDER BY TABLE_NAME"
                    ),
                    {"schema": target_schema},
                ).fetchall()
            ]

            concept_rows = None
            if table_exists(con, target_schema, "concept"):
                concept_rows = _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[concept]"
                )

        return {
            "database": str(sql_cfg.get("database")),
            "target_schema": target_schema,
            "target_rows": target_rows,
            "etl_tables": etl_tables,
            "staging_tables": staging_tables,
            "concept_rows": concept_rows,
        }
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inspection of the current OMOP validated build state."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = inspect_current_build(load_etl_config(args.config))
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("target_rows:", result["target_rows"])
    print("etl_tables:", result["etl_tables"])
    print("staging_tables:", result["staging_tables"])
    print("concept_rows:", result["concept_rows"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
