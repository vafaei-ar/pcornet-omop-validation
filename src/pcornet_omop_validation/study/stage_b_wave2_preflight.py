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
STUDY_DEFINITION = Path("study_definitions/stage_b_wave2_v1.json")

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

TARGET_TABLES = ("person", "drug_exposure", "measurement", "observation", "concept")
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


def _git(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return obj


def _schema(value: object, label: str) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {s!r}")
    return s


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _rows(con, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(text(sql)).mappings().all()]


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    repo_root = Path(__file__).resolve().parents[3]
    study_path = repo_root / STUDY_DEFINITION
    study = _load_json(study_path)
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Wave 2 preflight requires stage-b-wave2-v1")
    if study.get("status") != "prespecified_before_wave2_outcome_queries":
        raise RuntimeError("Wave 2 definition is not prespecified before outcome queries")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition frozen SHA mismatch")

    audit_dir = Path(config.audit_dir).expanduser().resolve()
    freeze_path = audit_dir / "clean_build_phase14_freeze_manifest.json"
    freeze = _load_json(freeze_path)
    if freeze.get("git_head") != FROZEN_ETL_SHA:
        raise RuntimeError("Final freeze manifest SHA mismatch")
    if freeze.get("dirty_worktree_entries") not in (0, None):
        raise RuntimeError("Final freeze manifest records a dirty worktree")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_source = [t for t in SOURCE_TABLES if not table_exists(con, source_schema, t)]
            missing_target = [t for t in TARGET_TABLES if not table_exists(con, target_schema, t)]
            missing_lineage = [t for t in LINEAGE_TABLES if not table_exists(con, target_schema, t)]
            if missing_source or missing_target or missing_lineage:
                raise RuntimeError(
                    f"Wave 2 prerequisites missing: source={missing_source}; "
                    f"target={missing_target}; lineage={missing_lineage}"
                )

            patient_linkage = {
                "person_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[person]"),
                "blank_person_source_value_rows": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM [{target_schema}].[person]
                    WHERE person_source_value IS NULL
                       OR LTRIM(RTRIM(CONVERT(nvarchar(255),person_source_value)))=''
                """),
                "duplicate_person_id_groups": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT person_id FROM [{target_schema}].[person]
                      GROUP BY person_id HAVING COUNT_BIG(*)>1
                    ) q
                """),
                "duplicate_person_source_value_groups": _scalar(con, f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT LTRIM(RTRIM(CONVERT(nvarchar(255),person_source_value))) source_value
                      FROM [{target_schema}].[person]
                      WHERE person_source_value IS NOT NULL
                        AND LTRIM(RTRIM(CONVERT(nvarchar(255),person_source_value)))<>''
                      GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255),person_source_value)))
                      HAVING COUNT_BIG(*)>1
                    ) q
                """),
            }
            if (
                patient_linkage["person_rows"] <= 0
                or patient_linkage["blank_person_source_value_rows"]
                or patient_linkage["duplicate_person_id_groups"]
                or patient_linkage["duplicate_person_source_value_groups"]
            ):
                raise RuntimeError(f"Patient bridge integrity failed: {patient_linkage}")
            patient_linkage["status"] = "matched_unique_source_bridge"

            source_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{t}]")
                for t in SOURCE_TABLES
            }
            target_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{t}]")
                for t in ("drug_exposure", "measurement", "observation")
            }
            lineage_counts = {
                t: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{t}]")
                for t in LINEAGE_TABLES
            }

            condition_cross_domain = {
                d: _scalar(con, f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[etl_condition_cross_domain_xwalk]
                    WHERE target_domain='{d}'
                """)
                for d in ("Drug", "Measurement", "Observation")
            }

            drug_by_family = _rows(con, f"""
                SELECT source_domain AS source_family,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)=0 THEN 1 ELSE 0 END) AS unresolved_rows,
                       COUNT_BIG(DISTINCT source_record_id) AS distinct_source_events
                FROM [{target_schema}].[etl_drug_event_route]
                GROUP BY source_domain
                ORDER BY source_domain
            """)
            drug_base_routes = sum(int(r["route_rows"]) for r in drug_by_family)
            drug_base_mapped = sum(int(r["mapped_rows"] or 0) for r in drug_by_family)
            drug_base_unresolved = sum(int(r["unresolved_rows"] or 0) for r in drug_by_family)
            drug_invalid_nonzero = _scalar(con, f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_drug_event_route] r
                LEFT JOIN [{target_schema}].[concept] c ON c.concept_id=r.target_concept_id
                WHERE COALESCE(r.target_concept_id,0)<>0
                  AND (c.concept_id IS NULL OR c.domain_id<>'Drug' OR c.standard_concept<>'S' OR c.invalid_reason IS NOT NULL)
            """)
            if drug_invalid_nonzero:
                raise RuntimeError(f"Drug route ledger has {drug_invalid_nonzero} invalid nonzero targets")
            if lineage_counts["etl_drug_exposure_xwalk"] != drug_base_routes:
                raise RuntimeError("Base Drug route and Drug xwalk counts do not reconcile")
            expected_drug_target = drug_base_routes + condition_cross_domain["Drug"]
            if target_counts["drug_exposure"] != expected_drug_target:
                raise RuntimeError(
                    "Drug target count does not reconcile to base Drug lineage plus Condition cross-domain rows"
                )
            drug = {
                "by_source_family": drug_by_family,
                "base_route_rows": drug_base_routes,
                "base_mapped_rows": drug_base_mapped,
                "base_unresolved_rows": drug_base_unresolved,
                "base_xwalk_rows": lineage_counts["etl_drug_exposure_xwalk"],
                "condition_cross_domain_rows": condition_cross_domain["Drug"],
                "target_rows": target_counts["drug_exposure"],
                "invalid_nonzero_standard_target_rows": drug_invalid_nonzero,
            }

            obs_clin_by_domain = _rows(con, f"""
                SELECT target_domain,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)=0 THEN 1 ELSE 0 END) AS unresolved_rows,
                       COUNT_BIG(DISTINCT source_obsclin_id) AS distinct_source_events
                FROM [{target_schema}].[etl_obs_clin_route]
                GROUP BY target_domain
                ORDER BY target_domain
            """)
            obs_clin_invalid_nonzero = _scalar(con, f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                LEFT JOIN [{target_schema}].[concept] c ON c.concept_id=r.target_concept_id
                WHERE COALESCE(r.target_concept_id,0)<>0
                  AND (c.concept_id IS NULL OR c.domain_id<>r.target_domain OR c.standard_concept<>'S' OR c.invalid_reason IS NOT NULL)
            """)
            if obs_clin_invalid_nonzero:
                raise RuntimeError("OBS_CLIN route ledger contains invalid nonzero targets")

            measurement_xwalk_by_family = _rows(con, f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_measurement_xwalk]
                GROUP BY source_family ORDER BY source_family
            """)
            observation_xwalk_by_family = _rows(con, f"""
                SELECT source_family, COUNT_BIG(*) AS rows
                FROM [{target_schema}].[etl_observation_xwalk]
                GROUP BY source_family ORDER BY source_family
            """)

            expected_measurement_target = lineage_counts["etl_measurement_xwalk"] + condition_cross_domain["Measurement"]
            expected_observation_target = lineage_counts["etl_observation_xwalk"] + condition_cross_domain["Observation"]
            if target_counts["measurement"] != expected_measurement_target:
                raise RuntimeError("Measurement target does not reconcile to base xwalk plus Condition cross-domain rows")
            if target_counts["observation"] != expected_observation_target:
                raise RuntimeError("Observation target does not reconcile to base xwalk plus Condition cross-domain rows")

            lab = {
                "source_rows": source_counts["PCORnet_LAB_RESULT_CM"],
                "eligible_result_date_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] WHERE RESULT_DATE IS NOT NULL"),
                "missing_result_date_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] WHERE RESULT_DATE IS NULL"),
            }
            vital = {
                "source_rows": source_counts["PCORnet_VITAL"],
                "numeric_value_rows": _scalar(con, f"""
                    SELECT COUNT_BIG(HT)+COUNT_BIG(WT)+COUNT_BIG(SYSTOLIC)+COUNT_BIG(DIASTOLIC)+COUNT_BIG(ORIGINAL_BMI)
                    FROM [{source_schema}].[PCORnet_VITAL]
                """),
                "categorical_value_rows": _scalar(con, f"""
                    SELECT COUNT_BIG(SMOKING)+COUNT_BIG(TOBACCO)+COUNT_BIG(TOBACCO_TYPE)
                    FROM [{source_schema}].[PCORnet_VITAL]
                """),
                "numeric_rows_missing_measure_date": _scalar(con, f"""
                    SELECT COALESCE(SUM(
                      CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END+
                      CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END+
                      CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END+
                      CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END+
                      CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END),0)
                    FROM [{source_schema}].[PCORnet_VITAL]
                    WHERE MEASURE_DATE IS NULL
                """),
            }
            obs_gen = {
                "source_rows": source_counts["PCORnet_OBS_GEN"],
                "observation_xwalk_rows": next(
                    (int(r["rows"]) for r in observation_xwalk_by_family if str(r["source_family"])=="OBS_GEN"),
                    0,
                ),
            }
            procedure_mo = _rows(con, f"""
                SELECT target_domain, COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN COALESCE(target_concept_id,0)=0 THEN 1 ELSE 0 END) AS unresolved_rows
                FROM [{target_schema}].[etl_procedure_event_route]
                WHERE target_domain IN ('Measurement','Observation')
                GROUP BY target_domain ORDER BY target_domain
            """)
            ucum_duplicate_groups = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT concept_code COLLATE Latin1_General_100_BIN2 AS concept_code
                  FROM [{target_schema}].[concept]
                  WHERE vocabulary_id='UCUM' AND domain_id='Unit'
                    AND standard_concept='S' AND invalid_reason IS NULL
                  GROUP BY concept_code COLLATE Latin1_General_100_BIN2
                  HAVING COUNT_BIG(*)>1
                ) q
            """)

            measurement_observation = {
                "obs_clin_by_target_domain": obs_clin_by_domain,
                "obs_clin_invalid_nonzero_standard_target_rows": obs_clin_invalid_nonzero,
                "measurement_xwalk_by_source_family": measurement_xwalk_by_family,
                "observation_xwalk_by_source_family": observation_xwalk_by_family,
                "lab_denominators": lab,
                "vital_denominators": vital,
                "obs_gen_denominators": obs_gen,
                "procedure_measurement_observation_routes": procedure_mo,
                "condition_cross_domain_rows": {
                    "Measurement": condition_cross_domain["Measurement"],
                    "Observation": condition_cross_domain["Observation"],
                },
                "measurement_base_xwalk_rows": lineage_counts["etl_measurement_xwalk"],
                "measurement_target_rows": target_counts["measurement"],
                "observation_base_xwalk_rows": lineage_counts["etl_observation_xwalk"],
                "observation_target_rows": target_counts["observation"],
                "active_standard_ucum_duplicate_code_groups_case_sensitive": ucum_duplicate_groups,
            }
    finally:
        engine.dispose()

    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    analysis_branch = _git(repo_root, "branch", "--show-current")
    porcelain = _git(repo_root, "status", "--porcelain")
    worktree_entries = [x for x in porcelain.splitlines() if x.strip()] if porcelain != "unknown" else []

    out = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance"
    )
    out.mkdir(parents=True, exist_ok=True)
    output_path = out / "stage_b_wave2_preflight.json"

    payload = {
        "stage": "stage_b_wave2_preflight",
        "status": "stage_b_wave2_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": "stage-b-wave2-v1",
        "study_definition_sha256": _sha256(study_path),
        "freeze_manifest_sha256": _sha256(freeze_path),
        "analysis_git_sha": analysis_sha,
        "analysis_git_branch": analysis_branch,
        "analysis_worktree_clean": not worktree_entries,
        "analysis_git_status_porcelain": worktree_entries,
        "database": str(sql_cfg.get("database")),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "patient_linkage": patient_linkage,
        "source_row_counts": source_counts,
        "target_row_counts": target_counts,
        "lineage_row_counts": lineage_counts,
        "drug": drug,
        "measurement_observation": measurement_observation,
        "rules": {
            "etl_immutable": True,
            "wave1_results_immutable": True,
            "primary_mapped_denominator_excludes_concept_zero": True,
            "lineage_role": "secondary_attribution_only",
            "ucum_exact_matching": "case_sensitive_binary_collation",
            "aggregate_only_outputs": True,
        },
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    payload["output_path"] = str(output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Stage B Wave 2 preflight and denominator inventory")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    r = run(args.config, args.output_dir)
    d = r["drug"]
    mo = r["measurement_observation"]
    print("status:", r["status"])
    print("frozen_etl_sha:", r["frozen_etl_sha"])
    print("study_definition:", r["study_definition"])
    print("study_definition_sha256:", r["study_definition_sha256"])
    print("analysis_git_sha:", r["analysis_git_sha"])
    print("analysis_worktree_clean:", r["analysis_worktree_clean"])
    print("patient_linkage_status:", r["patient_linkage"]["status"])
    print("drug_base_route_rows:", d["base_route_rows"])
    print("drug_base_mapped_rows:", d["base_mapped_rows"])
    print("drug_base_unresolved_rows:", d["base_unresolved_rows"])
    print("drug_condition_cross_domain_rows:", d["condition_cross_domain_rows"])
    print("drug_target_rows:", d["target_rows"])
    print("lab_source_rows:", mo["lab_denominators"]["source_rows"])
    print("lab_eligible_result_date_rows:", mo["lab_denominators"]["eligible_result_date_rows"])
    print("vital_numeric_value_rows:", mo["vital_denominators"]["numeric_value_rows"])
    print("vital_categorical_value_rows:", mo["vital_denominators"]["categorical_value_rows"])
    print("obs_clin_route_domains:", len(mo["obs_clin_by_target_domain"]))
    print("obs_gen_source_rows:", mo["obs_gen_denominators"]["source_rows"])
    print("measurement_target_rows:", mo["measurement_target_rows"])
    print("observation_target_rows:", mo["observation_target_rows"])
    print("ucum_duplicate_active_standard_code_groups_case_sensitive:", mo["active_standard_ucum_duplicate_code_groups_case_sensitive"])
    print("output:", r["output_path"])


if __name__ == "__main__":
    main()
