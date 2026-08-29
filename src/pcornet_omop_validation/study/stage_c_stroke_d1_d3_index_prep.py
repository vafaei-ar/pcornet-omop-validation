from __future__ import annotations

import argparse
from typing import Iterable

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists


def _schema(value: object, label: str) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {s!r}")
    return s


def _index_exists(con, schema: str, table: str, index_name: str) -> bool:
    return bool(
        con.execute(
            text(
                """
                SELECT 1
                FROM sys.indexes i
                JOIN sys.tables t ON t.object_id=i.object_id
                JOIN sys.schemas s ON s.schema_id=t.schema_id
                WHERE s.name=:schema AND t.name=:table AND i.name=:index_name
                """
            ),
            {"schema": schema, "table": table, "index_name": index_name},
        ).first()
    )


def _ensure_index(
    con,
    schema: str,
    table: str,
    index_name: str,
    columns: Iterable[str],
) -> str:
    if not table_exists(con, schema, table):
        raise RuntimeError(f"Missing required table [{schema}].[{table}]")
    if _index_exists(con, schema, table, index_name):
        return "already_present"
    cols = ", ".join(f"[{c}]" for c in columns)
    con.exec_driver_sql(
        f"CREATE NONCLUSTERED INDEX [{index_name}] ON [{schema}].[{table}] ({cols})"
    )
    return "created"


def run(config_path: str) -> None:
    cfg = load_etl_config(config_path)
    sql_cfg = cfg.raw["sqlserver"]
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    # These indexes are analysis-only physical accelerators for the prespecified
    # native-OMOP D1/D3 portability query. They do not alter rows, concepts,
    # dates, lineage, phenotype rules, or any frozen ETL transformation.
    specs = [
        (
            "condition_occurrence",
            "IX_stage_c_d1d3_condition_native",
            ("condition_concept_id", "visit_occurrence_id", "person_id"),
        ),
        (
            "procedure_occurrence",
            "IX_stage_c_d1d3_procedure_native",
            ("person_id", "procedure_date", "procedure_concept_id"),
        ),
        (
            "measurement",
            "IX_stage_c_d1d3_measurement_native",
            ("person_id", "measurement_date", "measurement_concept_id"),
        ),
        (
            "observation",
            "IX_stage_c_d1d3_observation_native",
            ("person_id", "observation_date", "observation_concept_id"),
        ),
    ]

    engine = make_engine(cfg)
    try:
        with engine.begin() as con:
            print("status: stage_c_stroke_d1_d3_index_prep_start")
            for table, index_name, columns in specs:
                print(f"progress: ensuring {index_name} on {target_schema}.{table}", flush=True)
                status = _ensure_index(con, target_schema, table, index_name, columns)
                print(f"  {status}")
            print("status: stage_c_stroke_d1_d3_index_prep_complete")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create analysis-only SQL Server indexes for Stage C stroke D1/D3 native-OMOP portability"
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
