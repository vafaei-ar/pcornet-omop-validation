from __future__ import annotations

"""Read-only source-date coverage audit for manuscript study-period wording.

This utility reads aggregate date coverage from the final PCORnet source tables used
by the publication analyses. It does not modify the source or target database and
does not write patient-level data.

The output is intended to support manuscript description of the source data period.
It reports both:

1. full source encounter-date coverage; and
2. a broad adult EI/IP overnight-encounter coverage matching the descriptive source
   population language used in the manuscript.

Neither summary changes any locked phenotype or outcome definition.
"""

import argparse
import json
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists
from pcornet_omop_validation.study.publication_analysis_manifest import FROZEN_ETL_SHA


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _identifier(value: object, *, label: str) -> str:
    s = str(value or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        raise ValueError(f"Unsafe {label}: {s!r}")
    return s


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"
        ),
        {"s": schema, "t": table},
    ).fetchall()
    return {str(row[0]).upper() for row in rows}


def _date_column_summary(con, schema: str, table: str, column: str) -> dict[str, Any]:
    schema = _identifier(schema, label="schema")
    table = _identifier(table, label="table")
    column = _identifier(column, label="column")
    row = con.execute(
        text(
            f"""
            SELECT
                COUNT_BIG(*) AS table_rows,
                SUM(CAST(CASE WHEN [{column}] IS NOT NULL THEN 1 ELSE 0 END AS bigint)) AS nonnull_rows,
                SUM(CAST(CASE WHEN [{column}] IS NOT NULL AND TRY_CONVERT(date,[{column}]) IS NULL THEN 1 ELSE 0 END AS bigint)) AS invalid_nonnull_rows,
                MIN(TRY_CONVERT(date,[{column}])) AS min_date,
                MAX(TRY_CONVERT(date,[{column}])) AS max_date
            FROM [{schema}].[{table}]
            """
        )
    ).mappings().one()
    return {str(k): _jsonable(v) for k, v in dict(row).items()}


def _optional_date_summary(
    con,
    schema: str,
    table: str,
    columns: list[str],
) -> dict[str, Any]:
    if not table_exists(con, schema, table):
        return {"status": "table_not_present", "table": table, "columns": {}}
    available = _columns(con, schema, table)
    out: dict[str, Any] = {}
    for column in columns:
        if column.upper() in available:
            out[column] = _date_column_summary(con, schema, table, column)
        else:
            out[column] = {"status": "column_not_present"}
    return {"status": "available", "table": table, "columns": out}


def _adult_ei_ip_overnight_summary(con, schema: str) -> dict[str, Any]:
    encounter = "PCORnet_ENCOUNTER"
    demographic = "PCORnet_DEMOGRAPHIC"
    if not table_exists(con, schema, encounter):
        return {"status": "encounter_table_not_present"}
    if not table_exists(con, schema, demographic):
        return {"status": "demographic_table_not_present"}

    enc_cols = _columns(con, schema, encounter)
    dem_cols = _columns(con, schema, demographic)
    required_enc = {"PATID", "ENC_TYPE", "ADMIT_DATE", "DISCHARGE_DATE"}
    required_dem = {"PATID", "BIRTH_DATE"}
    missing_enc = sorted(required_enc - enc_cols)
    missing_dem = sorted(required_dem - dem_cols)
    if missing_enc or missing_dem:
        return {
            "status": "required_columns_missing",
            "missing_encounter_columns": missing_enc,
            "missing_demographic_columns": missing_dem,
        }

    schema = _identifier(schema, label="schema")
    row = con.execute(
        text(
            f"""
            WITH eligible AS (
                SELECT
                    CONVERT(nvarchar(255), e.PATID) AS patid,
                    TRY_CONVERT(date, e.ADMIT_DATE) AS admit_date,
                    TRY_CONVERT(date, e.DISCHARGE_DATE) AS discharge_date
                FROM [{schema}].[{encounter}] e
                WHERE UPPER(LTRIM(RTRIM(CONVERT(nvarchar(32), e.ENC_TYPE)))) IN ('EI','IP')
                  AND TRY_CONVERT(date, e.ADMIT_DATE) IS NOT NULL
                  AND TRY_CONVERT(date, e.DISCHARGE_DATE) IS NOT NULL
                  AND DATEDIFF(day, TRY_CONVERT(date, e.ADMIT_DATE), TRY_CONVERT(date, e.DISCHARGE_DATE)) >= 1
                  AND EXISTS (
                      SELECT 1
                      FROM [{schema}].[{demographic}] d
                      WHERE d.PATID = e.PATID
                        AND TRY_CONVERT(date, d.BIRTH_DATE) IS NOT NULL
                        AND FLOOR(
                            DATEDIFF(
                                day,
                                TRY_CONVERT(date, d.BIRTH_DATE),
                                TRY_CONVERT(date, e.ADMIT_DATE)
                            ) / 365.0
                        ) >= 18
                  )
            )
            SELECT
                COUNT_BIG(*) AS encounters,
                COUNT_BIG(DISTINCT patid) AS patients,
                MIN(admit_date) AS first_admit_date,
                MAX(discharge_date) AS last_discharge_date
            FROM eligible
            """
        )
    ).mappings().one()
    return {
        "status": "available",
        "definition": (
            "Adult age >=18 at admission; ENC_TYPE EI or IP; valid admit/discharge dates; "
            "calendar-day stay >=1. This is a descriptive source-period audit, not a stroke phenotype."
        ),
        **{str(k): _jsonable(v) for k, v in dict(row).items()},
    }


def _period(start: Any, end: Any) -> dict[str, Any] | None:
    if not start or not end:
        return None
    start_text = str(start)
    end_text = str(end)
    try:
        start_year = int(start_text[:4])
        end_year = int(end_text[:4])
    except Exception:
        start_year = None
        end_year = None
    return {
        "start_date": start_text,
        "end_date": end_text,
        "calendar_years": (
            f"{start_year}-{end_year}" if start_year is not None and end_year is not None else None
        ),
    }


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _identifier(sql_cfg.get("source_schema", "dbo"), label="source schema")

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            database = str(con.execute(text("SELECT DB_NAME()")) .scalar_one())

            encounter = _optional_date_summary(
                con,
                source_schema,
                "PCORnet_ENCOUNTER",
                ["ADMIT_DATE", "DISCHARGE_DATE"],
            )
            diagnosis = _optional_date_summary(
                con,
                source_schema,
                "PCORnet_DIAGNOSIS",
                ["DX_DATE"],
            )
            enrollment = _optional_date_summary(
                con,
                source_schema,
                "PCORnet_ENROLLMENT",
                ["ENR_START_DATE", "ENR_END_DATE"],
            )
            adult_acute_care = _adult_ei_ip_overnight_summary(con, source_schema)
    finally:
        engine.dispose()

    encounter_columns = encounter.get("columns", {}) if encounter.get("status") == "available" else {}
    admit = encounter_columns.get("ADMIT_DATE", {})
    discharge = encounter_columns.get("DISCHARGE_DATE", {})
    full_encounter_period = _period(admit.get("min_date"), discharge.get("max_date"))
    adult_period = _period(
        adult_acute_care.get("first_admit_date"),
        adult_acute_care.get("last_discharge_date"),
    ) if adult_acute_care.get("status") == "available" else None

    repo_root = Path(__file__).resolve().parents[3]
    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    worktree = _git(repo_root, "status", "--porcelain") or ""

    payload: dict[str, Any] = {
        "status": "manuscript_source_date_coverage_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_worktree_clean": not bool(worktree.strip()),
        "database": database,
        "source_schema": source_schema,
        "source_date_coverage": {
            "encounter": encounter,
            "diagnosis": diagnosis,
            "enrollment": enrollment,
        },
        "adult_ei_ip_overnight_coverage": adult_acute_care,
        "candidate_manuscript_periods": {
            "full_source_encounter_coverage": full_encounter_period,
            "adult_ei_ip_overnight_coverage": adult_period,
        },
        "manuscript_guardrail": (
            "Use these aggregate dates only to describe the actual source-data coverage. "
            "Do not reinterpret them as a new eligibility rule or change the locked Stage C/D analyses. "
            "Review both the full encounter period and the broad adult EI/IP overnight period before choosing wording."
        ),
        "disclosure_review": {
            "read_only": True,
            "patient_level_rows_written": False,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "aggregate_dates_and_counts_only": True,
            "status": "passed",
        },
    }

    out_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else Path(config.audit_dir).resolve().parent / "publication_analysis" / "manuscript_metadata"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "manuscript_source_date_coverage.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("status:", payload["status"])
    print("frozen_etl_sha:", FROZEN_ETL_SHA)
    print("analysis_git_sha:", analysis_sha)
    print("analysis_worktree_clean:", payload["analysis_worktree_clean"])
    print("database:", database)
    print("source_schema:", source_schema)
    print("full_source_encounter_period:", json.dumps(full_encounter_period, sort_keys=True))
    print("adult_ei_ip_overnight_period:", json.dumps(adult_period, sort_keys=True))

    dx_cols = diagnosis.get("columns", {}) if diagnosis.get("status") == "available" else {}
    dx = dx_cols.get("DX_DATE") or {}
    print("diagnosis_dx_date_period:", json.dumps(_period(dx.get("min_date"), dx.get("max_date")), sort_keys=True))

    enr_cols = enrollment.get("columns", {}) if enrollment.get("status") == "available" else {}
    enr_start = enr_cols.get("ENR_START_DATE") or {}
    enr_end = enr_cols.get("ENR_END_DATE") or {}
    print("enrollment_period:", json.dumps(_period(enr_start.get("min_date"), enr_end.get("max_date")), sort_keys=True))
    print("output:", output_path)

    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only source-date coverage audit for manuscript study-period wording."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    run(args.config, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
