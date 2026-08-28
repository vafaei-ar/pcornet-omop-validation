from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_b_wave2_v1.json")


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _schema(value: object) -> str:
    value = str(value or "dbo")
    if not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError(f"Unsafe SQL Server schema: {value!r}")
    return value


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _rows(con, sql: str) -> list[dict[str, object]]:
    return [dict(r) for r in con.execute(text(sql)).mappings().all()]


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table
            """
        ),
        {"schema": schema, "table": table},
    ).fetchall()
    return {str(r[0]).lower() for r in rows}


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    study = json.loads(STUDY_DEFINITION.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Measurement/Observation preflight requires stage-b-wave2-v1")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition is not anchored to the frozen ETL SHA")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"))
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"))

    source_tables = (
        "PCORnet_LAB_RESULT_CM",
        "PCORnet_VITAL",
        "PCORnet_OBS_CLIN",
        "PCORnet_OBS_GEN",
    )
    target_tables = (
        "person",
        "measurement",
        "observation",
        "concept",
        "etl_measurement_xwalk",
        "etl_observation_xwalk",
        "etl_obs_clin_route",
        "etl_procedure_event_route",
        "etl_condition_cross_domain_xwalk",
    )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_source = [t for t in source_tables if not table_exists(con, source_schema, t)]
            missing_target = [t for t in target_tables if not table_exists(con, target_schema, t)]
            if missing_source or missing_target:
                raise RuntimeError(
                    f"Measurement/Observation prerequisites missing: source={missing_source}; target={missing_target}"
                )

            measurement_cols = _columns(con, target_schema, "etl_measurement_xwalk")
            observation_cols = _columns(con, target_schema, "etl_observation_xwalk")
            if "source_family" not in measurement_cols or "source_family" not in observation_cols:
                raise RuntimeError("Measurement/Observation xwalks must expose source_family for Wave 2 attribution")

            measurement_xwalk_by_family = _rows(
                con,
                f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_measurement_xwalk]
                GROUP BY source_family ORDER BY source_family
                """,
            )
            observation_xwalk_by_family = _rows(
                con,
                f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_observation_xwalk]
                GROUP BY source_family ORDER BY source_family
                """,
            )

            obs_clin_route_by_domain = _rows(
                con,
                f"""
                SELECT target_domain,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)=0 THEN 1 ELSE 0 END) AS unresolved_rows
                FROM [{target_schema}].[etl_obs_clin_route]
                GROUP BY target_domain ORDER BY target_domain
                """,
            )

            procedure_routes = _rows(
                con,
                f"""
                SELECT target_domain,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)=0 THEN 1 ELSE 0 END) AS unresolved_rows
                FROM [{target_schema}].[etl_procedure_event_route]
                WHERE target_domain IN ('Measurement','Observation')
                GROUP BY target_domain ORDER BY target_domain
                """,
            )

            condition_cross_domain = _rows(
                con,
                f"""
                SELECT target_domain, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_condition_cross_domain_xwalk]
                WHERE target_domain IN ('Measurement','Observation')
                GROUP BY target_domain ORDER BY target_domain
                """,
            )

            lab = {
                "source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]"),
                "eligible_result_date_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] WHERE RESULT_DATE IS NOT NULL"),
                "rows_with_nonblank_loinc": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                    WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), LAB_LOINC))), '') IS NOT NULL
                """),
                "rows_with_numeric_result": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                    WHERE TRY_CONVERT(float, RESULT_NUM) IS NOT NULL
                """),
                "rows_with_nonblank_unit": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                    WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), RESULT_UNIT))), '') IS NOT NULL
                """),
            }

            vital = {
                "source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_VITAL]"),
                "rows_with_measure_date": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_VITAL] WHERE MEASURE_DATE IS NOT NULL"),
                "numeric_value_rows": _scalar(con, f"""
                    SELECT COUNT_BIG(HT)+COUNT_BIG(WT)+COUNT_BIG(SYSTOLIC)+COUNT_BIG(DIASTOLIC)+COUNT_BIG(ORIGINAL_BMI)
                    FROM [{source_schema}].[PCORnet_VITAL]
                """),
                "categorical_value_rows": _scalar(con, f"""
                    SELECT COUNT_BIG(SMOKING)+COUNT_BIG(TOBACCO)+COUNT_BIG(TOBACCO_TYPE)
                    FROM [{source_schema}].[PCORnet_VITAL]
                """),
            }

            obs_gen = {
                "source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_OBS_GEN]"),
            }

            target = {
                "measurement_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[measurement]"),
                "observation_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[observation]"),
                "measurement_concept_zero_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[measurement] WHERE measurement_concept_id=0"),
                "observation_concept_zero_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[observation] WHERE observation_concept_id=0"),
                "measurement_unit_zero_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[measurement] WHERE unit_concept_id=0"),
            }

            active_standard_ucum_duplicate_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT concept_code COLLATE Latin1_General_100_BIN2 AS concept_code
                    FROM [{target_schema}].[concept]
                    WHERE vocabulary_id='UCUM' AND domain_id='Unit'
                      AND standard_concept='S' AND invalid_reason IS NULL
                    GROUP BY concept_code COLLATE Latin1_General_100_BIN2
                    HAVING COUNT_BIG(*)>1
                ) q
                """,
            )
            if active_standard_ucum_duplicate_groups:
                raise RuntimeError("Case-sensitive active Standard UCUM code uniqueness failed")
    finally:
        engine.dispose()

    out_root = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "measurement_observation"
    )
    out_root.mkdir(parents=True, exist_ok=True)

    payload = {
        "status": "stage_b_wave2_measurement_observation_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": "stage-b-wave2-v1",
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "lab": lab,
        "vital": vital,
        "obs_gen": obs_gen,
        "obs_clin_route_by_domain": obs_clin_route_by_domain,
        "measurement_xwalk_by_source_family": measurement_xwalk_by_family,
        "observation_xwalk_by_source_family": observation_xwalk_by_family,
        "procedure_measurement_observation_routes": procedure_routes,
        "condition_cross_domain_measurement_observation": condition_cross_domain,
        "target": target,
        "active_standard_ucum_duplicate_code_groups_case_sensitive": active_standard_ucum_duplicate_groups,
        "locked_layers": {
            "semantic_presence": "person + date + target domain + Standard concept",
            "numeric_value": "exact source-derived numeric conversion first; report absolute differences; no post-hoc tolerance",
            "unit": "compare only uniquely resolved active Standard UCUM under case-sensitive frozen policy",
            "categorical_value": "compare only where frozen exact Standard value mapping exists",
            "obs_gen": "descriptive concept-zero family; excluded from mapped semantic denominator",
        },
    }
    output = out_root / "stage_b_wave2_measurement_observation_preflight.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print("status: stage_b_wave2_measurement_observation_preflight_ready")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {payload['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {payload['analysis_worktree_clean']}")
    print(f"lab_source_rows: {lab['source_rows']}")
    print(f"lab_numeric_result_rows: {lab['rows_with_numeric_result']}")
    print(f"lab_nonblank_unit_rows: {lab['rows_with_nonblank_unit']}")
    print(f"vital_numeric_value_rows: {vital['numeric_value_rows']}")
    print(f"vital_categorical_value_rows: {vital['categorical_value_rows']}")
    print(f"measurement_target_rows: {target['measurement_rows']}")
    print(f"observation_target_rows: {target['observation_rows']}")
    print(f"measurement_unit_zero_rows: {target['measurement_unit_zero_rows']}")
    print(f"output: {output}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage B Wave 2 Measurement/Observation preflight")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
