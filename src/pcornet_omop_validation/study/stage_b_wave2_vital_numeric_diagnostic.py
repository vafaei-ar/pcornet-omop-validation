from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
FIELDS = ("HT", "WT", "SYSTOLIC", "DIASTOLIC", "ORIGINAL_BMI")


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _schema(value: object, label: str) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {s!r}")
    return s


def _rows(con, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(text(sql)).mappings().all()]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent
        / "publication_analysis"
        / "stage_b_patient_concordance"
        / "measurement_observation"
    )
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_VITAL"),
                (target_schema, "measurement"),
                (target_schema, "etl_measurement_xwalk"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            print("progress: reading VITAL source numeric column metadata", flush=True)
            metadata = _rows(con, f"""
                SELECT c.COLUMN_NAME AS source_field,c.DATA_TYPE AS data_type,
                       c.NUMERIC_PRECISION AS numeric_precision,c.NUMERIC_SCALE AS numeric_scale,
                       c.CHARACTER_MAXIMUM_LENGTH AS character_maximum_length
                FROM INFORMATION_SCHEMA.COLUMNS c
                WHERE c.TABLE_SCHEMA='{source_schema}'
                  AND c.TABLE_NAME='PCORnet_VITAL'
                  AND c.COLUMN_NAME IN ('HT','WT','SYSTOLIC','DIASTOLIC','ORIGINAL_BMI')
                ORDER BY CASE c.COLUMN_NAME
                  WHEN 'HT' THEN 1 WHEN 'WT' THEN 2 WHEN 'SYSTOLIC' THEN 3
                  WHEN 'DIASTOLIC' THEN 4 WHEN 'ORIGINAL_BMI' THEN 5 ELSE 99 END
            """)

            print("progress: reproducing direct-source and frozen-ETL expanded numeric expressions", flush=True)
            con.exec_driver_sql("SET NOCOUNT ON")
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#vital_numeric_diag') IS NOT NULL DROP TABLE #vital_numeric_diag")
            con.exec_driver_sql(f"""
                WITH expanded AS (
                  SELECT
                    LTRIM(RTRIM(CONVERT(nvarchar(255),v.VITALID))) AS source_record_id,
                    x.source_field,
                    TRY_CONVERT(float,x.source_value) AS etl_expanded_value
                  FROM [{source_schema}].[PCORnet_VITAL] v
                  CROSS APPLY (VALUES
                    ('HT',1,v.HT),
                    ('WT',2,v.WT),
                    ('SYSTOLIC',3,v.SYSTOLIC),
                    ('DIASTOLIC',4,v.DIASTOLIC),
                    ('ORIGINAL_BMI',5,v.ORIGINAL_BMI)
                  ) x(source_field,field_order,source_value)
                  WHERE x.source_value IS NOT NULL AND v.MEASURE_DATE IS NOT NULL
                ),
                direct_source AS (
                  SELECT
                    LTRIM(RTRIM(CONVERT(nvarchar(255),v.VITALID))) AS source_record_id,
                    x.source_field,
                    TRY_CONVERT(float,x.source_text) AS direct_source_value
                  FROM [{source_schema}].[PCORnet_VITAL] v
                  CROSS APPLY (VALUES
                    ('HT',CONVERT(nvarchar(255),v.HT)),
                    ('WT',CONVERT(nvarchar(255),v.WT)),
                    ('SYSTOLIC',CONVERT(nvarchar(255),v.SYSTOLIC)),
                    ('DIASTOLIC',CONVERT(nvarchar(255),v.DIASTOLIC)),
                    ('ORIGINAL_BMI',CONVERT(nvarchar(255),v.ORIGINAL_BMI))
                  ) x(source_field,source_text)
                  WHERE x.source_text IS NOT NULL AND v.MEASURE_DATE IS NOT NULL
                )
                SELECT x.source_field,d.direct_source_value,e.etl_expanded_value,
                       m.value_as_number AS target_value
                INTO #vital_numeric_diag
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m
                  ON m.measurement_id=x.measurement_id
                JOIN direct_source d
                  ON d.source_record_id=x.source_record_id AND d.source_field=x.source_field
                JOIN expanded e
                  ON e.source_record_id=x.source_record_id AND e.source_field=x.source_field
                WHERE x.source_family='VITAL'
                  AND x.source_field IN ('HT','WT','SYSTOLIC','DIASTOLIC','ORIGINAL_BMI');

                CREATE INDEX IX_vital_numeric_diag_field ON #vital_numeric_diag(source_field);
            """)

            field_rows = _rows(con, """
                SELECT source_field,
                       COUNT_BIG(*) AS rows,
                       SUM(CASE WHEN direct_source_value=target_value THEN 1 ELSE 0 END) AS direct_target_exact_rows,
                       SUM(CASE WHEN direct_source_value<>target_value OR target_value IS NULL THEN 1 ELSE 0 END) AS direct_target_mismatch_rows,
                       SUM(CASE WHEN etl_expanded_value=target_value THEN 1 ELSE 0 END) AS expanded_target_exact_rows,
                       SUM(CASE WHEN etl_expanded_value<>target_value OR target_value IS NULL THEN 1 ELSE 0 END) AS expanded_target_mismatch_rows,
                       SUM(CASE WHEN direct_source_value=etl_expanded_value THEN 1 ELSE 0 END) AS direct_expanded_exact_rows,
                       SUM(CASE WHEN direct_source_value<>etl_expanded_value THEN 1 ELSE 0 END) AS direct_expanded_mismatch_rows,
                       SUM(CASE WHEN direct_source_value<>target_value AND etl_expanded_value=target_value THEN 1 ELSE 0 END) AS mismatches_explained_by_etl_expansion_rows,
                       SUM(CASE WHEN direct_source_value<>target_value AND etl_expanded_value<>target_value THEN 1 ELSE 0 END) AS mismatches_not_explained_by_etl_expansion_rows,
                       MAX(ABS(direct_source_value-target_value)) AS max_direct_target_abs_difference,
                       AVG(ABS(direct_source_value-target_value)) AS mean_direct_target_abs_difference,
                       MAX(ABS(direct_source_value-etl_expanded_value)) AS max_direct_expanded_abs_difference
                FROM #vital_numeric_diag
                GROUP BY source_field
                ORDER BY CASE source_field
                  WHEN 'HT' THEN 1 WHEN 'WT' THEN 2 WHEN 'SYSTOLIC' THEN 3
                  WHEN 'DIASTOLIC' THEN 4 WHEN 'ORIGINAL_BMI' THEN 5 ELSE 99 END
            """)

            diff_bins = _rows(con, """
                WITH d AS (
                  SELECT source_field,ABS(direct_source_value-target_value) AS abs_diff
                  FROM #vital_numeric_diag
                  WHERE target_value IS NOT NULL AND direct_source_value<>target_value
                )
                SELECT source_field,
                       CASE
                         WHEN abs_diff<=1e-12 THEN '(0,1e-12]'
                         WHEN abs_diff<=1e-9 THEN '(1e-12,1e-9]'
                         WHEN abs_diff<=1e-6 THEN '(1e-9,1e-6]'
                         WHEN abs_diff<=1e-3 THEN '(1e-6,1e-3]'
                         WHEN abs_diff<=0.1 THEN '(1e-3,0.1]'
                         WHEN abs_diff<=1.0 THEN '(0.1,1]'
                         ELSE '>1'
                       END AS difference_bin,
                       COUNT_BIG(*) AS rows
                FROM d
                GROUP BY source_field,
                         CASE
                           WHEN abs_diff<=1e-12 THEN '(0,1e-12]'
                           WHEN abs_diff<=1e-9 THEN '(1e-12,1e-9]'
                           WHEN abs_diff<=1e-6 THEN '(1e-9,1e-6]'
                           WHEN abs_diff<=1e-3 THEN '(1e-6,1e-3]'
                           WHEN abs_diff<=0.1 THEN '(1e-3,0.1]'
                           WHEN abs_diff<=1.0 THEN '(0.1,1]'
                           ELSE '>1'
                         END
                ORDER BY source_field,difference_bin
            """)

            overall = dict(con.execute(text("""
                SELECT COUNT_BIG(*) AS rows,
                       SUM(CASE WHEN direct_source_value<>target_value OR target_value IS NULL THEN 1 ELSE 0 END) AS direct_target_mismatch_rows,
                       SUM(CASE WHEN etl_expanded_value<>target_value OR target_value IS NULL THEN 1 ELSE 0 END) AS expanded_target_mismatch_rows,
                       SUM(CASE WHEN direct_source_value<>etl_expanded_value THEN 1 ELSE 0 END) AS direct_expanded_mismatch_rows,
                       SUM(CASE WHEN direct_source_value<>target_value AND etl_expanded_value=target_value THEN 1 ELSE 0 END) AS mismatches_explained_by_etl_expansion_rows,
                       SUM(CASE WHEN direct_source_value<>target_value AND etl_expanded_value<>target_value THEN 1 ELSE 0 END) AS mismatches_not_explained_by_etl_expansion_rows,
                       MAX(ABS(direct_source_value-target_value)) AS max_direct_target_abs_difference
                FROM #vital_numeric_diag
            """)).mappings().one())
    finally:
        engine.dispose()

    for row in field_rows:
        for key in (
            "rows","direct_target_exact_rows","direct_target_mismatch_rows",
            "expanded_target_exact_rows","expanded_target_mismatch_rows",
            "direct_expanded_exact_rows","direct_expanded_mismatch_rows",
            "mismatches_explained_by_etl_expansion_rows",
            "mismatches_not_explained_by_etl_expansion_rows",
        ):
            row[key] = int(row[key] or 0)
        for key in (
            "max_direct_target_abs_difference","mean_direct_target_abs_difference",
            "max_direct_expanded_abs_difference",
        ):
            row[key] = None if row[key] is None else float(row[key])
    for row in diff_bins:
        row["rows"] = int(row["rows"] or 0)
    for key in (
        "rows","direct_target_mismatch_rows","expanded_target_mismatch_rows",
        "direct_expanded_mismatch_rows","mismatches_explained_by_etl_expansion_rows",
        "mismatches_not_explained_by_etl_expansion_rows",
    ):
        overall[key] = int(overall[key] or 0)
    overall["max_direct_target_abs_difference"] = (
        None if overall["max_direct_target_abs_difference"] is None
        else float(overall["max_direct_target_abs_difference"])
    )

    summary = {
        "status":"stage_b_wave2_vital_numeric_diagnostic_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "purpose":"Determine whether VITAL exact-value discrepancies arise from the frozen ETL CROSS APPLY VALUES common-type expression or from another transformation difference. Aggregate-only; no patient-level output.",
        "comparison":{
            "direct_source_value":"TRY_CONVERT(float, CONVERT(nvarchar(255), each native VITAL field))",
            "etl_expanded_value":"TRY_CONVERT(float, source_value) after the same CROSS APPLY VALUES expression used by the frozen ETL",
            "target_value":"measurement.value_as_number aligned through frozen Measurement xwalk",
        },
        "overall":overall,
        "source_column_metadata":metadata,
        "by_field":field_rows,
        "difference_bins":diff_bins,
    }
    _write_csv(out/"vital_numeric_diagnostic_by_field.csv", field_rows)
    _write_csv(out/"vital_numeric_diagnostic_difference_bins.csv", diff_bins)
    _write_csv(out/"vital_numeric_source_column_metadata.csv", metadata)
    output = out/"stage_b_wave2_vital_numeric_diagnostic.json"
    output.write_text(json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8")

    print("status: stage_b_wave2_vital_numeric_diagnostic_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"rows: {overall['rows']}")
    print(f"direct_target_mismatch_rows: {overall['direct_target_mismatch_rows']}")
    print(f"expanded_target_mismatch_rows: {overall['expanded_target_mismatch_rows']}")
    print(f"direct_expanded_mismatch_rows: {overall['direct_expanded_mismatch_rows']}")
    print(f"mismatches_explained_by_etl_expansion_rows: {overall['mismatches_explained_by_etl_expansion_rows']}")
    print(f"mismatches_not_explained_by_etl_expansion_rows: {overall['mismatches_not_explained_by_etl_expansion_rows']}")
    print(f"max_direct_target_abs_difference: {overall['max_direct_target_abs_difference']}")
    print(f"output: {output}")
    return summary


def main() -> None:
    parser=argparse.ArgumentParser(description="Stage B Wave 2 aggregate VITAL numeric discrepancy diagnostic")
    parser.add_argument("--config",required=True)
    parser.add_argument("--output-dir")
    args=parser.parse_args()
    run(args.config,args.output_dir)


if __name__=="__main__":
    main()
