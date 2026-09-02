from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_e_statistical_model_reproducibility_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
STAGE_D_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _schema(v: object) -> str:
    s = str(v or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe schema: {s!r}")
    return s


def _columns(con, schema: str, table: str) -> list[str]:
    rows = con.execute(text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t ORDER BY ORDINAL_POSITION"), {"s": schema, "t": table}).fetchall()
    return [str(r[0]) for r in rows]


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("status") != "prespecified_before_stage_e_outcome_model_queries":
        raise RuntimeError("Stage E definition is not in the prespecified state")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage E frozen ETL SHA mismatch")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    required = [
        (source_schema, "PCORnet_DEMOGRAPHIC"),
        (source_schema, "PCORnet_ENCOUNTER"),
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_ENROLLMENT"),
        (target_schema, "person"),
        (target_schema, "visit_occurrence"),
        (target_schema, "condition_occurrence"),
        (target_schema, "observation_period"),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "etl_condition_occurrence_xwalk"),
    ]

    engine = make_engine(cfg)
    table_inventory: dict[str, object] = {}
    try:
        with engine.connect() as con:
            for schema, table in required:
                exists = table_exists(con, schema, table)
                if not exists:
                    raise RuntimeError(f"Missing required table [{schema}].[{table}]")
                cols = _columns(con, schema, table)
                table_inventory[f"{schema}.{table}"] = {"exists": True, "columns": cols}

            source_demographic_cols = {c.upper() for c in _columns(con, source_schema, "PCORnet_DEMOGRAPHIC")}
            source_encounter_cols = {c.upper() for c in _columns(con, source_schema, "PCORnet_ENCOUNTER")}
            target_person_cols = {c.upper() for c in _columns(con, target_schema, "person")}
            target_visit_cols = {c.upper() for c in _columns(con, target_schema, "visit_occurrence")}

            feature_checks = {
                "source_birth_date": "BIRTH_DATE" in source_demographic_cols,
                "source_sex": "SEX" in source_demographic_cols,
                "source_encounter_type": "ENC_TYPE" in source_encounter_cols,
                "source_admit_date": "ADMIT_DATE" in source_encounter_cols,
                "source_discharge_date": "DISCHARGE_DATE" in source_encounter_cols,
                "target_birth": bool({"BIRTH_DATETIME", "YEAR_OF_BIRTH"} & target_person_cols),
                "target_gender_concept": "GENDER_CONCEPT_ID" in target_person_cols,
                "target_visit_start": "VISIT_START_DATE" in target_visit_cols,
                "target_visit_end": "VISIT_END_DATE" in target_visit_cols,
                "target_visit_concept": "VISIT_CONCEPT_ID" in target_visit_cols,
            }
            if not all(feature_checks.values()):
                raise RuntimeError(f"Stage E feature preflight failed: {feature_checks}")
    finally:
        engine.dispose()

    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_e_statistical_model"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "stage_e_statistical_model_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "inherited_d0_definition_sha256": _sha256(D0_PATH),
        "inherited_stage_d_definition_sha256": _sha256(STAGE_D_PATH),
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "outcome_query_performed": False,
        "model_fit_performed": False,
        "feature_checks": feature_checks,
        "required_table_inventory": table_inventory,
        "locked_core_features": study["features"]["core_model_features"],
        "locked_prediction_models": [m["name"] for m in study["prediction_models"]],
        "disclosure_review": {"aggregate_only_outputs": True, "patient_identifiers_written": False, "row_level_phi_written": False, "status": "passed"},
    }
    out = out_dir / "stage_e_statistical_model_preflight.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_e_statistical_model_preflight_ready")
    print(f"analysis_git_sha: {payload['analysis_git_sha']}")
    print(f"study_definition_sha256: {payload['study_definition_sha256']}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Outcome-free Stage E statistical/model reproducibility preflight")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
