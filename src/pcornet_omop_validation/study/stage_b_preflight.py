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
STUDY_DEFINITION_RELATIVE = Path("study_definitions/stage_b_v1.json")

SOURCE_TABLES = (
    "PCORnet_ENCOUNTER",
    "PCORnet_DIAGNOSIS",
    "PCORnet_CONDITION",
    "PCORnet_PROCEDURES",
    "PCORnet_DEATH",
)

TARGET_TABLES = (
    "person",
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

LINEAGE_TABLES = (
    "etl_visit_occurrence_xwalk",
    "etl_condition_event_route_v2",
    "etl_condition_occurrence_xwalk",
    "etl_obs_clin_condition_xwalk",
    "etl_procedure_condition_xwalk",
    "etl_condition_cross_domain_xwalk",
    "etl_procedure_event_route",
    "etl_procedure_occurrence_xwalk",
    "etl_measurement_xwalk",
    "etl_observation_xwalk",
    "etl_drug_exposure_xwalk",
    "etl_device_exposure_xwalk",
    "etl_specimen_xwalk",
    "etl_death_xwalk",
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


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def run_stage_b_preflight(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    repo_root = Path(__file__).resolve().parents[3]
    study_definition_path = repo_root / STUDY_DEFINITION_RELATIVE
    if not study_definition_path.is_file():
        raise FileNotFoundError(f"Locked Stage B definition missing: {study_definition_path}")

    study_definition = _load_object(study_definition_path)
    if study_definition.get("study_definition") != "stage-b-v1":
        raise RuntimeError("Stage B preflight requires locked study definition stage-b-v1")
    if study_definition.get("status") != "locked_before_patient_level_comparison":
        raise RuntimeError("Stage B v1 definition is not marked locked before comparison")
    if study_definition.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage B definition frozen ETL SHA does not match publication freeze")

    audit_dir = Path(config.audit_dir).expanduser().resolve()
    freeze_manifest_path = audit_dir / "clean_build_phase14_freeze_manifest.json"
    if not freeze_manifest_path.is_file():
        raise FileNotFoundError(f"Final ETL freeze manifest missing: {freeze_manifest_path}")
    freeze_manifest = _load_object(freeze_manifest_path)
    if freeze_manifest.get("git_head") != FROZEN_ETL_SHA:
        raise RuntimeError(
            "Final freeze manifest does not identify the publication ETL SHA: "
            f"{freeze_manifest.get('git_head')!r}"
        )
    if freeze_manifest.get("dirty_worktree_entries") not in (0, None):
        raise RuntimeError("Final ETL freeze manifest records a dirty worktree")

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_source = [
                name for name in SOURCE_TABLES if not table_exists(con, source_schema, name)
            ]
            missing_target = [
                name for name in TARGET_TABLES if not table_exists(con, target_schema, name)
            ]
            missing_lineage = [
                name for name in LINEAGE_TABLES if not table_exists(con, target_schema, name)
            ]
            if missing_source or missing_target or missing_lineage:
                raise RuntimeError(
                    "Stage B Wave 1 prerequisites missing: "
                    f"source={missing_source}; target={missing_target}; lineage={missing_lineage}"
                )

            person_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[person]"
            )
            blank_person_source = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[person]
                WHERE person_source_value IS NULL
                   OR LTRIM(RTRIM(CONVERT(nvarchar(255), person_source_value))) = ''
                """,
            )
            duplicate_person_ids = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT person_id
                  FROM [{target_schema}].[person]
                  GROUP BY person_id HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
            duplicate_person_source_values = _scalar(
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
            )
            if person_rows <= 0:
                raise RuntimeError("Stage B requires a populated frozen OMOP Person table")
            if blank_person_source or duplicate_person_ids or duplicate_person_source_values:
                raise RuntimeError(
                    "Patient linkage integrity failed: "
                    f"blank_source={blank_person_source}, duplicate_person_id_groups={duplicate_person_ids}, "
                    f"duplicate_person_source_value_groups={duplicate_person_source_values}"
                )

            source_row_counts = {
                name: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{name}]")
                for name in SOURCE_TABLES
            }
            target_row_counts = {
                name: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{name}]")
                for name in TARGET_TABLES
            }
            lineage_row_counts = {
                name: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{name}]")
                for name in LINEAGE_TABLES
            }
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

    payload = {
        "stage": "stage_b_wave1_preflight",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "stage_b_wave1_preflight_ready",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": "stage-b-v1",
        "study_definition_path": str(study_definition_path),
        "study_definition_sha256": _sha256(study_definition_path),
        "freeze_manifest_path": str(freeze_manifest_path),
        "freeze_manifest_sha256": _sha256(freeze_manifest_path),
        "analysis_git_sha": analysis_sha,
        "analysis_git_branch": analysis_branch,
        "analysis_worktree_clean": not worktree_entries,
        "analysis_git_status_porcelain": worktree_entries,
        "database": str(sql_cfg.get("database")),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "patient_linkage": {
            "person_rows": person_rows,
            "blank_person_source_value_rows": blank_person_source,
            "duplicate_person_id_groups": duplicate_person_ids,
            "duplicate_person_source_value_groups": duplicate_person_source_values,
            "status": "matched_unique_source_bridge",
        },
        "source_row_counts": source_row_counts,
        "target_row_counts": target_row_counts,
        "lineage_row_counts": lineage_row_counts,
        "rules": {
            "primary_comparison": "independent_cdm_native",
            "lineage_role": "secondary_discordance_attribution",
            "etl_retuning_from_concordance": "prohibited",
        },
    }
    output_path = output_root / "stage_b_wave1_preflight.json"
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["output_path"] = str(output_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only Stage B Wave 1 patient-semantic-concordance preflight."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    result = run_stage_b_preflight(args.config, output_dir=args.output_dir)
    print("status:", result["status"])
    print("frozen_etl_sha:", result["frozen_etl_sha"])
    print("study_definition:", result["study_definition"])
    print("study_definition_sha256:", result["study_definition_sha256"])
    print("analysis_git_sha:", result["analysis_git_sha"])
    print("analysis_worktree_clean:", result["analysis_worktree_clean"])
    print("patient_linkage_status:", result["patient_linkage"]["status"])
    print("source_tables:", len(result["source_row_counts"]))
    print("target_tables:", len(result["target_row_counts"]))
    print("lineage_tables:", len(result["lineage_row_counts"]))
    print("output:", result["output_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
