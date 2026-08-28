from __future__ import annotations

import argparse
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
STUDY_DEFINITION_RELATIVE = Path("study_definitions/stage_b_wave2_v1.json")

SOURCE_TABLES = (
    "PCORnet_PRESCRIBING",
    "PCORnet_DISPENSING",
    "PCORnet_MED_ADMIN",
    "PCORnet_IMMUNIZATION",
    "PCORnet_PROCEDURES",
    "PCORnet_LAB_RESULT_CM",
    "PCORnet_VITAL",
    "PCORnet_OBS_CLIN",
    "PCORnet_OBS_GEN",
)

TARGET_TABLES = (
    "person",
    "drug_exposure",
    "measurement",
    "observation",
    "concept",
)

LINEAGE_TABLES = (
    "etl_drug_event_route",
    "etl_drug_exposure_xwalk",
    "etl_obs_clin_route",
    "etl_measurement_xwalk",
    "etl_observation_xwalk",
    "etl_procedure_event_route",
    "etl_condition_event_route_v2",
    "etl_condition_cross_domain_xwalk",
    "etl_visit_occurrence_xwalk",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return value


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if not schema.replace("_", "a").isalnum() or schema[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _rows(con, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(text(sql)).mappings().all()]


def run_stage_b_wave2_preflight(
    config_path: str, output_dir: str | None = None
) -> dict[str, Any]:
    config = load_etl_config(config_path)
    repo_root = Path(__file__).resolve().parents[3]
    study_path = repo_root / STUDY_DEFINITION_RELATIVE
    if not study_path.is_file():
        raise FileNotFoundError(f"Locked Wave 2 definition missing: {study_path}")

    study = _load_object(study_path)
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Wave 2 preflight requires study definition stage-b-wave2-v1")
    if study.get("status") != "prespecified_before_wave2_outcome_queries":
        raise RuntimeError("Wave 2 definition is not marked prespecified before outcome queries")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition does not point to the publication ETL freeze")

    audit_dir = Path(config.audit_dir).expanduser().resolve()
    freeze_manifest_path = audit_dir / "clean_build_phase14_freeze_manifest.json"
    if not freeze_manifest_path.is_file():
        raise FileNotFoundError(f"Final ETL freeze manifest missing: {freeze_manifest_path}")
    freeze_manifest = _load_object(freeze_manifest_path)
    if freeze_manifest.get("git_head") != FROZEN_ETL_SHA:
        raise RuntimeError("Freeze manifest git_head does not match the publication ETL SHA")
    if freeze_manifest.get("dirty_worktree_entries") not in (0, None):
        raise RuntimeError("Freeze manifest records a dirty ETL worktree")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_source = [
                t for t in SOURCE_TABLES if not table_exists(con, source_schema, t)
            ]
            missing_target = [
                t for t in TARGET_TABLES if not table_exists(con, target_schema, t)
            ]
            missing_lineage = [
                t for t in LINEAGE_TABLES if not table_exists(con, target_schema, t)
            ]
            if missing_source or missing_target or missing_lineage:
                raise RuntimeError(
                    "Stage B Wave 2 prerequisites missing: "
                    f"source={missing_source}; target={missing_target}; lineage={missing_lineage}"
                )

            person_integrity = {
                "person_rows": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[person]"
                ),
                "blank_person_source_value_rows": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[person]
                    WHERE person_source_value IS NULL
                       OR LTRIM(RTRIM(CONVERT(nvarchar(255), person_source_value))) = ''
                    """,
                ),
                "duplicate_person_id_groups": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT person_id
                      FROM [{target_schema}].[person]
                      GROUP BY person_id HAVING COUNT_BIG(*) > 1
                    ) q
                    """,
                ),
                "duplicate_person_source_value_groups": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), person_source_value))) AS source_value
                      FROM [{target_schema}].[person]
                      WHERE person_source_value IS NOT NULL
                        AND LTRIM(RTRIM(CONVERT(nvarchar(255), person_source_value))) <> ''
                      GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), person_source_value)))
                      HAVING COUNT_BIG(*) > 1
                    ) q
                    """,
                ),
            }
            if (
                person_integrity["person_rows"] <= 0
                or person_integrity["blank_person_source_value_rows"]
                or person_integrity["duplicate_person_id_groups"]
                or person_integrity["duplicate_person_source_value_groups"]
            ):
                raise RuntimeError(f"Wave 2 patient bridge integrity failed: {person_integrity}")
            person_integrity["status"] = "matched_unique_source_bridge"

            source_row_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{t}]")
                for t in SOURCE_TABLES
            }
            target_row_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{t}]")
                for t in ("drug_exposure", "measurement", "observation")
            }
            lineage_row_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{t}]")
                for t in LINEAGE_TABLES
            }

            drug_by_family = _rows(
                con,
                f"""
                SELECT
                  source_domain AS source_family,
                  COUNT_BIG(*) AS route_rows,
                  SUM(CASE WHEN COALESCE(target_concept_id,0) <> 0 THEN 1 ELSE 0 END) AS mapped_rows,
                  SUM(CASE WHEN COALESCE(target_concept_id,0) = 0 THEN 1 ELSE 0 END) AS unresolved_rows,
                  COUNT_BIG(DISTINCT source_record_id) AS distinct_source_events
                FROM [{target_schema}].[etl_drug_event_route]
                GROUP BY source_domain
                ORDER BY source_domain
                """,
            )
            drug_totals = {
                "route_rows": sum(int(r["route_rows"]) for r in drug_by_family),
                "mapped_rows": sum(int(r["mapped_rows"] or 0) for r in drug_by_family),
                "unresolved_rows": sum(int(r["unresolved_rows"] or 0) for r in drug_by_family),
                "distinct_source_event_sum": sum(
                    int(r["distinct_source_events"] or 0) for r in drug_by_family
                ),
                "xwalk_rows": lineage_row_counts["etl_drug_exposure_xwalk"],
                "target_rows": target_row_counts["drug_exposure"],
            }
            drug_invalid_nonzero_targets = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_drug_event_route] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE COALESCE(r.target_concept_id,0) <> 0
                  AND (
                    c.concept_id IS NULL
                    OR c.domain_id <> 'Drug'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if drug_invalid_nonzero_targets:
                raise RuntimeError(
                    f"Drug route ledger has {drug_invalid_nonzero_targets} invalid nonzero targets"
                )
            if drug_totals["xwalk_rows"] != drug_totals["target_rows"]:
                raise RuntimeError("Drug Exposure target/xwalk counts do not reconcile")
            if drug_totals["route_rows"] != drug_totals["target_rows"]:
                raise RuntimeError("Drug route/target counts do not reconcile")

            obs_clin_by_domain = _rows(
                con,
                f"""
                SELECT
                  target_domain,
                  COUNT_BIG(*) AS route_rows,
                  SUM(CASE WHEN COALESCE(target_concept_id,0) <> 0 THEN 1 ELSE 0 END) AS mapped_rows,
                  SUM(CASE WHEN COALESCE(target_concept_id,0) = 0 THEN 1 ELSE 0 END) AS unresolved_rows,
                  COUNT_BIG(DISTINCT source_obsclin_id) AS distinct_source_events
                FROM [{target_schema}].[etl_obs_clin_route]
                GROUP BY target_domain
                ORDER BY target_domain
                """,
            )
            obs_clin_invalid_nonzero_targets = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE COALESCE(r.target_concept_id,0) <> 0
                  AND (
                    c.concept_id IS NULL
                    OR c.domain_id <> r.target_domain
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if obs_clin_invalid_nonzero_targets:
                raise RuntimeError(
                    "OBS_CLIN route ledger contains invalid nonzero Standard targets"
                )

            measurement_xwalk_by_family = _rows(
                con,
                f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_measurement_xwalk]
                GROUP BY source_family
                ORDER BY source_family
                """,
            )
            observation_xwalk_by_family = _rows(
                con,
                f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_observation_xwalk]
                GROUP BY source_family
                ORDER BY source_family
                """,
            )

            lab_denominators = {
                "source_rows": source_row_counts["PCORnet_LAB_RESULT_CM"],
                "eligible_result_date_rows": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] WHERE RESULT_DATE IS NOT NULL",
                ),
                "missing_result_date_rows": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] WHERE RESULT_DATE IS NULL",
                ),
            }
            vital_denominators = {
                "source_rows": source_row_counts["PCORnet_VITAL"],
                "numeric_value_rows": _scalar(
                    con,
                    f"""
                    SELECT
                      COUNT_BIG(HT)+COUNT_BIG(WT)+COUNT_BIG(SYSTOLIC)+
                      COUNT_BIG(DIASTOLIC)+COUNT_BIG(ORIGINAL_BMI)
                    FROM [{source_schema}].[PCORnet_VITAL]
                    """,
                ),
                "categorical_value_rows": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(SMOKING)+COUNT_BIG(TOBACCO)+COUNT_BIG(TOBACCO_TYPE)
                    FROM [{source_schema}].[PCORnet_VITAL]
                    """,
                ),
                "numeric_rows_missing_measure_date": _scalar(
                    con,
                    f"""
                    SELECT COALESCE(SUM(
                      CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                      CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                      CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                      CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                      CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                    ),0)
                    FROM [{source_schema}].[PCORnet_VITAL]
                    WHERE MEASURE_DATE IS NULL
                    """,
                ),
            }
            obs_gen_denominators = {
                "source_rows": source_row_counts["PCORnet_OBS_GEN"],
                "observation_xwalk_rows": next(
                    (
                        int(r["rows"])
                        for r in observation_xwalk_by_family
                        if str(r["source_family"]) == "OBS_GEN"
                    ),
                    0,
                ),
            }

            procedure_measurement_observation_routes = _rows(
                con,
                f"""
                SELECT target_domain, COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0) <> 0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0) = 0 THEN 1 ELSE 0 END) AS unresolved_rows
                FROM [{target_schema}].[etl_procedure_event_route]
                WHERE target_domain IN ('Measurement','Observation')
                GROUP BY target_domain
                ORDER BY target_domain
                """,
            )
            condition_cross_domain_measurement_observation = _rows(
                con,
                f"""
                SELECT target_domain, COUNT_BIG(*) AS xwalk_rows
                FROM [{target_schema}].[etl_condition_cross_domain_xwalk]
                WHERE target_domain IN ('Measurement','Observation')
                GROUP BY target_domain
                ORDER BY target_domain
                """,
            )

            active_standard_ucum_duplicate_code_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT concept_code COLLATE Latin1_General_100_BIN2 AS concept_code
                  FROM [{target_schema}].[concept]
                  WHERE vocabulary_id='UCUM'
                    AND domain_id='Unit'
                    AND standard_concept='S'
                    AND invalid_reason IS NULL
                  GROUP BY concept_code COLLATE Latin1_General_100_BIN2
                  HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
    finally:
        engine.dispose()

    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    analysis_branch = _git(repo_root, "branch", "--show-current")
    porcelain = _git(repo_root, "status", "--porcelain") or ""
    worktree_entries = [line for line in porcelain.splitlines() if line.strip()]

    output_root = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "stage_b_wave2_preflight.json"

    payload: dict[str, Any] = {
        "stage": "stage_b_wave2_preflight",
        "status": "stage_b_wave2_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": "stage-b-wave2-v1",
        "study_definition_path": str(study_path),
        "study_definition_sha256": _sha256(study_path),
        "freeze_manifest_path": str(freeze_manifest_path),
        "freeze_manifest_sha256": _sha256(freeze_manifest_path),
        "analysis_git_sha": analysis_sha,
        "analysis_git_branch": analysis_branch,
        "analysis_worktree_clean": not worktree_entries,
        "analysis_git_status_porcelain": worktree_entries,
        "database": str(sql_cfg.get("database")),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "patient_linkage": person_integrity,
        "source_row_counts": source_row_counts,
        "target_row_counts": target_row_counts,
        "lineage_row_counts": lineage_row_counts,
        "drug": {
            "by_source_family": drug_by_family,
            "totals": drug_totals,
            "invalid_nonzero_standard_target_rows": drug_invalid_nonzero_targets,
        },
        "measurement_observation": {
            "obs_clin_by_target_domain": obs_clin_by_domain,
            "obs_clin_invalid_nonzero_standard_target_rows": obs_clin_invalid_nonzero_targets,
            "measurement_xwalk_by_source_family": measurement_xwalk_by_family,
            "observation_xwalk_by_source_family": observation_xwalk_by_family,
            "lab_denominators": lab_denominators,
            "vital_denominators": vital_denominators,
            "obs_gen_denominators": obs_gen_denominators,
            "procedure_measurement_observation_routes": procedure_measurement_observation_routes,
            "condition_cross_domain_measurement_observation": condition_cross_domain_measurement_observation,
            "active_standard_ucum_duplicate_code_groups_case_sensitive": active_standard_ucum_duplicate_code_groups,
        },
        "rules": {
            "etl_immutable": True,
            "wave1_results_immutable": True,
            "primary_mapped_denominator_excludes_concept_zero": True,
            "lineage_role": "secondary_attribution_only",
            "ucum_exact_matching": "case_sensitive_binary_collation",
            "no_row_level_phi_outputs": True,
        },
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    payload["output_path"] = str(output_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only Stage B Wave 2 preflight and denominator inventory."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    result = run_stage_b_wave2_preflight(args.config, output_dir=args.output_dir)
    drug = result["drug"]["totals"]
    mo = result["measurement_observation"]
    print("status:", result["status"])
    print("frozen_etl_sha:", result["frozen_etl_sha"])
    print("study_definition:", result["study_definition"])
    print("study_definition_sha256:", result["study_definition_sha256"])
    print("analysis_git_sha:", result["analysis_git_sha"])
    print("analysis_worktree_clean:", result["analysis_worktree_clean"])
    print("patient_linkage_status:", result["patient_linkage"]["status"])
    print("drug_route_rows:", drug["route_rows"])
    print("drug_mapped_rows:", drug["mapped_rows"])
    print("drug_unresolved_rows:", drug["unresolved_rows"])
    print("lab_source_rows:", mo["lab_denominators"]["source_rows"])
    print("lab_eligible_result_date_rows:", mo["lab_denominators"]["eligible_result_date_rows"])
    print("vital_numeric_value_rows:", mo["vital_denominators"]["numeric_value_rows"])
    print("vital_categorical_value_rows:", mo["vital_denominators"]["categorical_value_rows"])
    print("obs_clin_route_domains:", len(mo["obs_clin_by_target_domain"]))
    print("obs_gen_source_rows:", mo["obs_gen_denominators"]["source_rows"])
    print("ucum_duplicate_active_standard_code_groups_case_sensitive:", mo["active_standard_ucum_duplicate_code_groups_case_sensitive"])
    print("output:", result["output_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
