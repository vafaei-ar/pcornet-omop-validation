from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_b_wave2_v1.json")

VITAL_UNITS = {
    "HT": "[in_i]",
    "WT": "[lb_av]",
    "SYSTOLIC": "mm[Hg]",
    "DIASTOLIC": "mm[Hg]",
    "ORIGINAL_BMI": "kg/m2",
}

SMOKING_VALUES = {
    "01": 45881517,
    "02": 45884037,
    "03": 45883458,
    "04": 45879404,
    "05": 45881518,
    "06": 45885135,
    "07": 45884038,
    "08": 45878118,
}

TOBACCO_TYPE_VALUES = {
    "01": 42530793,
    "03": 42531020,
    "05": 42530756,
}


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema(value: object, label: str) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {s!r}")
    return s


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


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
    study_path = Path(STUDY_DEFINITION)
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Measurement/Observation value layers require stage-b-wave2-v1")
    if study.get("status") != "prespecified_before_wave2_outcome_queries":
        raise RuntimeError("Wave 2 definition is not prespecified")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition frozen SHA mismatch")

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

    required = (
        (source_schema, "PCORnet_LAB_RESULT_CM"),
        (source_schema, "PCORnet_VITAL"),
        (source_schema, "PCORnet_OBS_CLIN"),
        (target_schema, "measurement"),
        (target_schema, "observation"),
        (target_schema, "concept"),
        (target_schema, "etl_measurement_xwalk"),
        (target_schema, "etl_observation_xwalk"),
    )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            con.exec_driver_sql("SET NOCOUNT ON")

            print("progress: materializing directly comparable numeric source-target pairs", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#numeric_pairs') IS NOT NULL DROP TABLE #numeric_pairs")
            con.exec_driver_sql(f"""
                SELECT CAST('LAB_RESULT_CM' AS varchar(32)) AS source_family,
                       CAST('Measurement' AS varchar(16)) AS target_domain,
                       TRY_CONVERT(float,l.RESULT_NUM) AS source_value,
                       m.value_as_number AS target_value
                INTO #numeric_pairs
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON x.source_family='LAB_RESULT_CM'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                WHERE x.source_family='LAB_RESULT_CM'
                  AND m.measurement_concept_id<>0
                  AND TRY_CONVERT(float,l.RESULT_NUM) IS NOT NULL

                UNION ALL

                SELECT 'LAB_RESULT_CM','Observation',TRY_CONVERT(float,l.RESULT_NUM),o.value_as_number
                FROM [{target_schema}].[etl_observation_xwalk] x
                JOIN [{target_schema}].[observation] o ON o.observation_id=x.observation_id
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON x.source_family='LAB_RESULT_CM'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                WHERE x.source_family='LAB_RESULT_CM'
                  AND o.observation_concept_id<>0
                  AND TRY_CONVERT(float,l.RESULT_NUM) IS NOT NULL

                UNION ALL

                SELECT 'OBS_CLIN','Measurement',TRY_CONVERT(float,s.OBSCLIN_RESULT_NUM),m.value_as_number
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] s
                  ON x.source_family='OBS_CLIN'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),s.OBSCLINID)))
                WHERE x.source_family='OBS_CLIN'
                  AND m.measurement_concept_id<>0
                  AND TRY_CONVERT(float,s.OBSCLIN_RESULT_NUM) IS NOT NULL

                UNION ALL

                SELECT 'OBS_CLIN','Observation',TRY_CONVERT(float,s.OBSCLIN_RESULT_NUM),o.value_as_number
                FROM [{target_schema}].[etl_observation_xwalk] x
                JOIN [{target_schema}].[observation] o ON o.observation_id=x.observation_id
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] s
                  ON x.source_family='OBS_CLIN'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),s.OBSCLINID)))
                WHERE x.source_family='OBS_CLIN'
                  AND o.observation_concept_id<>0
                  AND TRY_CONVERT(float,s.OBSCLIN_RESULT_NUM) IS NOT NULL

                UNION ALL

                SELECT 'VITAL','Measurement',
                       TRY_CONVERT(float,
                         CASE x.source_field
                           WHEN 'HT' THEN CONVERT(nvarchar(255),v.HT)
                           WHEN 'WT' THEN CONVERT(nvarchar(255),v.WT)
                           WHEN 'SYSTOLIC' THEN CONVERT(nvarchar(255),v.SYSTOLIC)
                           WHEN 'DIASTOLIC' THEN CONVERT(nvarchar(255),v.DIASTOLIC)
                           WHEN 'ORIGINAL_BMI' THEN CONVERT(nvarchar(255),v.ORIGINAL_BMI)
                         END),
                       m.value_as_number
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                JOIN [{source_schema}].[PCORnet_VITAL] v
                  ON x.source_family='VITAL'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),v.VITALID)))
                WHERE x.source_family='VITAL'
                  AND x.source_field IN ('HT','WT','SYSTOLIC','DIASTOLIC','ORIGINAL_BMI')
                  AND TRY_CONVERT(float,
                         CASE x.source_field
                           WHEN 'HT' THEN CONVERT(nvarchar(255),v.HT)
                           WHEN 'WT' THEN CONVERT(nvarchar(255),v.WT)
                           WHEN 'SYSTOLIC' THEN CONVERT(nvarchar(255),v.SYSTOLIC)
                           WHEN 'DIASTOLIC' THEN CONVERT(nvarchar(255),v.DIASTOLIC)
                           WHEN 'ORIGINAL_BMI' THEN CONVERT(nvarchar(255),v.ORIGINAL_BMI)
                         END) IS NOT NULL;

                CREATE INDEX IX_numeric_pairs_family_domain ON #numeric_pairs(source_family,target_domain);
            """)

            numeric_rows = _rows(con, """
                SELECT source_family,target_domain,
                       COUNT_BIG(*) AS directly_comparable_rows,
                       SUM(CASE WHEN target_value IS NOT NULL AND source_value=target_value THEN 1 ELSE 0 END) AS exact_match_rows,
                       SUM(CASE WHEN target_value IS NULL OR source_value<>target_value THEN 1 ELSE 0 END) AS mismatch_rows,
                       MAX(CASE WHEN target_value IS NOT NULL THEN ABS(source_value-target_value) END) AS max_absolute_difference,
                       AVG(CASE WHEN target_value IS NOT NULL THEN ABS(source_value-target_value) END) AS mean_absolute_difference
                FROM #numeric_pairs
                GROUP BY source_family,target_domain
                ORDER BY source_family,target_domain
            """)
            for row in numeric_rows:
                row["directly_comparable_rows"] = int(row["directly_comparable_rows"] or 0)
                row["exact_match_rows"] = int(row["exact_match_rows"] or 0)
                row["mismatch_rows"] = int(row["mismatch_rows"] or 0)
                row["exact_match_percent"] = _pct(row["exact_match_rows"], row["directly_comparable_rows"])
                row["max_absolute_difference"] = None if row["max_absolute_difference"] is None else float(row["max_absolute_difference"])
                row["mean_absolute_difference"] = None if row["mean_absolute_difference"] is None else float(row["mean_absolute_difference"])

            numeric_total = sum(r["directly_comparable_rows"] for r in numeric_rows)
            numeric_exact = sum(r["exact_match_rows"] for r in numeric_rows)
            numeric_mismatch = sum(r["mismatch_rows"] for r in numeric_rows)
            max_abs_values = [r["max_absolute_difference"] for r in numeric_rows if r["max_absolute_difference"] is not None]
            numeric_max_abs = max(max_abs_values) if max_abs_values else None

            print("progress: evaluating case-sensitive UCUM unit coverage and agreement", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#ucum') IS NOT NULL DROP TABLE #ucum")
            con.exec_driver_sql(f"""
                WITH candidates AS (
                  SELECT concept_code COLLATE Latin1_General_100_BIN2 AS concept_code,
                         concept_id,
                         COUNT_BIG(*) OVER (
                           PARTITION BY concept_code COLLATE Latin1_General_100_BIN2
                         ) AS n
                  FROM [{target_schema}].[concept]
                  WHERE vocabulary_id='UCUM' AND domain_id='Unit'
                    AND standard_concept='S' AND invalid_reason IS NULL
                )
                SELECT concept_code,MAX(CASE WHEN n=1 THEN concept_id END) AS unit_concept_id
                INTO #ucum
                FROM candidates
                GROUP BY concept_code;
                CREATE UNIQUE CLUSTERED INDEX IX_ucum ON #ucum(concept_code);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#unit_pairs') IS NOT NULL DROP TABLE #unit_pairs")
            con.exec_driver_sql(f"""
                SELECT CAST('LAB_RESULT_CM' AS varchar(32)) AS source_family,
                       NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),l.RESULT_UNIT))),'') COLLATE Latin1_General_100_BIN2 AS source_unit,
                       u.unit_concept_id AS expected_unit_concept_id,
                       m.unit_concept_id AS target_unit_concept_id
                INTO #unit_pairs
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON x.source_family='LAB_RESULT_CM'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                LEFT JOIN #ucum u
                  ON u.concept_code=NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),l.RESULT_UNIT))),'') COLLATE Latin1_General_100_BIN2
                WHERE x.source_family='LAB_RESULT_CM'
                  AND NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),l.RESULT_UNIT))),'') IS NOT NULL

                UNION ALL

                SELECT 'OBS_CLIN',
                       NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),s.OBSCLIN_RESULT_UNIT))),'') COLLATE Latin1_General_100_BIN2,
                       u.unit_concept_id,m.unit_concept_id
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] s
                  ON x.source_family='OBS_CLIN'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),s.OBSCLINID)))
                LEFT JOIN #ucum u
                  ON u.concept_code=NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),s.OBSCLIN_RESULT_UNIT))),'') COLLATE Latin1_General_100_BIN2
                WHERE x.source_family='OBS_CLIN'
                  AND NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50),s.OBSCLIN_RESULT_UNIT))),'') IS NOT NULL

                UNION ALL

                SELECT 'VITAL',
                       CASE x.source_field
                         WHEN 'HT' THEN '[in_i]'
                         WHEN 'WT' THEN '[lb_av]'
                         WHEN 'SYSTOLIC' THEN 'mm[Hg]'
                         WHEN 'DIASTOLIC' THEN 'mm[Hg]'
                         WHEN 'ORIGINAL_BMI' THEN 'kg/m2'
                       END COLLATE Latin1_General_100_BIN2,
                       u.unit_concept_id,m.unit_concept_id
                FROM [{target_schema}].[etl_measurement_xwalk] x
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=x.measurement_id
                LEFT JOIN #ucum u
                  ON u.concept_code=(CASE x.source_field
                         WHEN 'HT' THEN '[in_i]'
                         WHEN 'WT' THEN '[lb_av]'
                         WHEN 'SYSTOLIC' THEN 'mm[Hg]'
                         WHEN 'DIASTOLIC' THEN 'mm[Hg]'
                         WHEN 'ORIGINAL_BMI' THEN 'kg/m2'
                       END) COLLATE Latin1_General_100_BIN2
                WHERE x.source_family='VITAL'
                  AND x.source_field IN ('HT','WT','SYSTOLIC','DIASTOLIC','ORIGINAL_BMI');

                CREATE INDEX IX_unit_pairs_family ON #unit_pairs(source_family);
            """)

            unit_rows = _rows(con, """
                SELECT source_family,
                       COUNT_BIG(*) AS source_rows_with_unit_semantics,
                       SUM(CASE WHEN expected_unit_concept_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved_standard_ucum_rows,
                       SUM(CASE WHEN expected_unit_concept_id IS NULL THEN 1 ELSE 0 END) AS unresolved_ucum_rows,
                       SUM(CASE WHEN expected_unit_concept_id IS NOT NULL
                                  AND target_unit_concept_id=expected_unit_concept_id THEN 1 ELSE 0 END) AS exact_agreement_rows,
                       SUM(CASE WHEN expected_unit_concept_id IS NOT NULL
                                  AND target_unit_concept_id<>expected_unit_concept_id THEN 1 ELSE 0 END) AS resolved_disagreement_rows
                FROM #unit_pairs
                GROUP BY source_family
                ORDER BY source_family
            """)
            for row in unit_rows:
                for key in (
                    "source_rows_with_unit_semantics",
                    "resolved_standard_ucum_rows",
                    "unresolved_ucum_rows",
                    "exact_agreement_rows",
                    "resolved_disagreement_rows",
                ):
                    row[key] = int(row[key] or 0)
                row["standard_ucum_coverage_percent"] = _pct(
                    row["resolved_standard_ucum_rows"], row["source_rows_with_unit_semantics"]
                )
                row["exact_agreement_among_resolved_percent"] = _pct(
                    row["exact_agreement_rows"], row["resolved_standard_ucum_rows"]
                )

            unit_source_rows = sum(r["source_rows_with_unit_semantics"] for r in unit_rows)
            unit_resolved = sum(r["resolved_standard_ucum_rows"] for r in unit_rows)
            unit_unresolved = sum(r["unresolved_ucum_rows"] for r in unit_rows)
            unit_exact = sum(r["exact_agreement_rows"] for r in unit_rows)
            unit_disagree = sum(r["resolved_disagreement_rows"] for r in unit_rows)

            print("progress: evaluating frozen exact categorical value mappings", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#categorical_pairs') IS NOT NULL DROP TABLE #categorical_pairs")
            con.exec_driver_sql(f"""
                SELECT x.source_field,
                       CONVERT(nvarchar(50),
                         CASE x.source_field
                           WHEN 'SMOKING' THEN v.SMOKING
                           WHEN 'TOBACCO' THEN v.TOBACCO
                           WHEN 'TOBACCO_TYPE' THEN v.TOBACCO_TYPE
                         END) AS source_value,
                       CASE
                         WHEN x.source_field='SMOKING' THEN
                           CASE v.SMOKING
                             WHEN '01' THEN 45881517 WHEN '02' THEN 45884037
                             WHEN '03' THEN 45883458 WHEN '04' THEN 45879404
                             WHEN '05' THEN 45881518 WHEN '06' THEN 45885135
                             WHEN '07' THEN 45884038 WHEN '08' THEN 45878118
                             ELSE 0 END
                         WHEN x.source_field='TOBACCO_TYPE' THEN
                           CASE v.TOBACCO_TYPE
                             WHEN '01' THEN 42530793 WHEN '03' THEN 42531020
                             WHEN '05' THEN 42530756 ELSE 0 END
                         ELSE 0
                       END AS expected_value_concept_id,
                       o.value_as_concept_id AS target_value_concept_id
                INTO #categorical_pairs
                FROM [{target_schema}].[etl_observation_xwalk] x
                JOIN [{target_schema}].[observation] o ON o.observation_id=x.observation_id
                JOIN [{source_schema}].[PCORnet_VITAL] v
                  ON x.source_family='VITAL'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),v.VITALID)))
                WHERE x.source_family='VITAL'
                  AND x.source_field IN ('SMOKING','TOBACCO','TOBACCO_TYPE');
                CREATE INDEX IX_categorical_pairs_field ON #categorical_pairs(source_field);
            """)

            categorical_rows = _rows(con, """
                SELECT source_field,
                       COUNT_BIG(*) AS categorical_rows,
                       SUM(CASE WHEN expected_value_concept_id<>0 THEN 1 ELSE 0 END) AS mapped_value_rows,
                       SUM(CASE WHEN expected_value_concept_id=0 THEN 1 ELSE 0 END) AS concept_zero_policy_rows,
                       SUM(CASE WHEN expected_value_concept_id<>0
                                  AND target_value_concept_id=expected_value_concept_id THEN 1 ELSE 0 END) AS exact_mapped_agreement_rows,
                       SUM(CASE WHEN expected_value_concept_id<>0
                                  AND target_value_concept_id<>expected_value_concept_id THEN 1 ELSE 0 END) AS mapped_disagreement_rows,
                       SUM(CASE WHEN expected_value_concept_id=0
                                  AND target_value_concept_id=0 THEN 1 ELSE 0 END) AS expected_zero_target_zero_rows,
                       SUM(CASE WHEN expected_value_concept_id=0
                                  AND target_value_concept_id<>0 THEN 1 ELSE 0 END) AS unexpected_nonzero_target_rows
                FROM #categorical_pairs
                GROUP BY source_field
                ORDER BY source_field
            """)
            for row in categorical_rows:
                for key in (
                    "categorical_rows",
                    "mapped_value_rows",
                    "concept_zero_policy_rows",
                    "exact_mapped_agreement_rows",
                    "mapped_disagreement_rows",
                    "expected_zero_target_zero_rows",
                    "unexpected_nonzero_target_rows",
                ):
                    row[key] = int(row[key] or 0)
                row["mapped_value_coverage_percent"] = _pct(
                    row["mapped_value_rows"], row["categorical_rows"]
                )
                row["exact_agreement_among_mapped_percent"] = _pct(
                    row["exact_mapped_agreement_rows"], row["mapped_value_rows"]
                )

            categorical_total = sum(r["categorical_rows"] for r in categorical_rows)
            categorical_mapped = sum(r["mapped_value_rows"] for r in categorical_rows)
            categorical_zero = sum(r["concept_zero_policy_rows"] for r in categorical_rows)
            categorical_exact = sum(r["exact_mapped_agreement_rows"] for r in categorical_rows)
            categorical_disagree = sum(r["mapped_disagreement_rows"] for r in categorical_rows)
            categorical_unexpected_nonzero = sum(r["unexpected_nonzero_target_rows"] for r in categorical_rows)
    finally:
        engine.dispose()

    numeric_summary = {
        "directly_comparable_rows": numeric_total,
        "exact_match_rows": numeric_exact,
        "mismatch_rows": numeric_mismatch,
        "exact_match_percent": _pct(numeric_exact, numeric_total),
        "max_absolute_difference": numeric_max_abs,
        "rule": "Exact TRY_CONVERT(float, source numeric) versus stored value_as_number; no tolerance applied.",
    }
    unit_summary = {
        "source_rows_with_unit_semantics": unit_source_rows,
        "resolved_standard_ucum_rows": unit_resolved,
        "unresolved_ucum_rows": unit_unresolved,
        "standard_ucum_coverage_percent": _pct(unit_resolved, unit_source_rows),
        "exact_agreement_rows": unit_exact,
        "resolved_disagreement_rows": unit_disagree,
        "exact_agreement_among_resolved_percent": _pct(unit_exact, unit_resolved),
        "rule": "Exact active Standard UCUM concept resolution under Latin1_General_100_BIN2; unresolved units are coverage, not semantic-presence failures.",
    }
    categorical_summary = {
        "categorical_rows": categorical_total,
        "mapped_value_rows": categorical_mapped,
        "concept_zero_policy_rows": categorical_zero,
        "mapped_value_coverage_percent": _pct(categorical_mapped, categorical_total),
        "exact_mapped_agreement_rows": categorical_exact,
        "mapped_disagreement_rows": categorical_disagree,
        "unexpected_nonzero_target_rows_for_zero_policy": categorical_unexpected_nonzero,
        "exact_agreement_among_mapped_percent": _pct(categorical_exact, categorical_mapped),
        "rule": "Only prespecified exact Standard value mappings for SMOKING and TOBACCO_TYPE enter mapped categorical agreement; TOBACCO and unsupported values remain concept zero.",
    }

    summary = {
        "status": "stage_b_wave2_measurement_observation_value_layers_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_definition": "stage-b-wave2-v1",
        "study_definition_sha256": _sha256(study_path),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_branch": _git("branch", "--show-current"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "method_note": (
            "These are secondary value/unit layers after semantic-presence concordance. "
            "Frozen xwalks provide deterministic source-target row alignment only; they do not define the primary event identity or denominator."
        ),
        "numeric_value": numeric_summary,
        "numeric_value_by_source_family_domain": numeric_rows,
        "unit": unit_summary,
        "unit_by_source_family": unit_rows,
        "categorical_value": categorical_summary,
        "categorical_value_by_field": categorical_rows,
    }

    (out / "stage_b_wave2_measurement_observation_value_layers.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    _write_csv(out / "measurement_observation_numeric_value.csv", numeric_rows)
    _write_csv(out / "measurement_observation_unit.csv", unit_rows)
    _write_csv(out / "measurement_observation_categorical_value.csv", categorical_rows)

    print("status: stage_b_wave2_measurement_observation_value_layers_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"numeric_directly_comparable_rows: {numeric_total}")
    print(f"numeric_exact_match_rows: {numeric_exact}")
    print(f"numeric_mismatch_rows: {numeric_mismatch}")
    print(f"numeric_max_absolute_difference: {numeric_max_abs}")
    print(f"unit_source_rows_with_semantics: {unit_source_rows}")
    print(f"unit_resolved_standard_ucum_rows: {unit_resolved}")
    print(f"unit_unresolved_ucum_rows: {unit_unresolved}")
    print(f"unit_exact_agreement_rows: {unit_exact}")
    print(f"unit_resolved_disagreement_rows: {unit_disagree}")
    print(f"categorical_rows: {categorical_total}")
    print(f"categorical_mapped_value_rows: {categorical_mapped}")
    print(f"categorical_concept_zero_policy_rows: {categorical_zero}")
    print(f"categorical_exact_mapped_agreement_rows: {categorical_exact}")
    print(f"categorical_mapped_disagreement_rows: {categorical_disagree}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage B Wave 2 Measurement/Observation numeric, UCUM unit, and categorical value layers"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
