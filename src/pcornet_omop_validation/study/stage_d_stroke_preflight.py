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
from pcornet_omop_validation.etl.visit_occurrence import VISIT_CONCEPT_MAP

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
STAGE_C_SUMMARY = Path("results/publication_analysis/stage_c_phenotypes/stage_c_stroke_phenotype_summary.json")


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


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
        {"s": schema, "t": table},
    ).fetchall()
    return {str(r[0]).upper() for r in rows}


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    d0 = json.loads(D0_PATH.read_text(encoding="utf-8"))
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA or d0.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage D or inherited D0 definition is not anchored to the frozen ETL")
    if study.get("status") != "prespecified_before_stage_d_cross_cdm_outcome_queries":
        raise RuntimeError("Stage D definition is not marked prespecified")
    if not STAGE_C_SUMMARY.exists():
        raise RuntimeError(f"Missing completed Stage C summary: {STAGE_C_SUMMARY}")
    stage_c = json.loads(STAGE_C_SUMMARY.read_text(encoding="utf-8"))
    if stage_c.get("status") != "stage_c_stroke_phenotype_summary_complete" or not stage_c.get("all_invariants_matched"):
        raise RuntimeError("Stage C summary is not complete with matched invariants")
    if stage_c.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage C summary is not anchored to the frozen ETL")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    required = {
        (source_schema, "PCORnet_DIAGNOSIS"): {"PATID", "ENCOUNTERID", "DIAGNOSISID", "DX", "DX_DATE", "PDX"},
        (source_schema, "PCORnet_ENCOUNTER"): {"PATID", "ENCOUNTERID", "ADMIT_DATE", "DISCHARGE_DATE", "ENC_TYPE"},
        (source_schema, "PCORnet_DEMOGRAPHIC"): {"PATID", "BIRTH_DATE"},
        (source_schema, "PCORnet_ENROLLMENT"): {"PATID", "ENR_START_DATE", "ENR_END_DATE"},
        (target_schema, "person"): {"PERSON_ID", "PERSON_SOURCE_VALUE", "BIRTH_DATETIME"},
        (target_schema, "visit_occurrence"): {"VISIT_OCCURRENCE_ID", "PERSON_ID", "VISIT_CONCEPT_ID", "VISIT_START_DATE", "VISIT_END_DATE"},
        (target_schema, "observation_period"): {"PERSON_ID", "OBSERVATION_PERIOD_START_DATE", "OBSERVATION_PERIOD_END_DATE"},
        (target_schema, "condition_occurrence"): {"CONDITION_OCCURRENCE_ID", "PERSON_ID", "VISIT_OCCURRENCE_ID", "CONDITION_START_DATE"},
        (target_schema, "etl_visit_occurrence_xwalk"): {"ENCOUNTERID", "VISIT_OCCURRENCE_ID"},
        (target_schema, "etl_condition_occurrence_xwalk"): {"SOURCE_DOMAIN", "SOURCE_RECORD_ID", "CONDITION_OCCURRENCE_ID"},
        (target_schema, "concept"): {"CONCEPT_ID", "DOMAIN_ID", "STANDARD_CONCEPT", "INVALID_REASON"},
    }
    checks: list[dict[str, object]] = []
    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            for (schema, table), cols in required.items():
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Missing required table [{schema}].[{table}]")
                have = _columns(con, schema, table)
                missing = sorted(cols - have)
                checks.append({"schema": schema, "table": table, "missing_required_columns": missing})
                if missing:
                    raise RuntimeError(f"Missing required columns in [{schema}].[{table}]: {missing}")

            acute_concepts = {
                "ED": int(VISIT_CONCEPT_MAP["ED"]),
                "EI": int(VISIT_CONCEPT_MAP["EI"]),
                "IP": int(VISIT_CONCEPT_MAP["IP"]),
            }
            values = ",".join(str(v) for v in acute_concepts.values())
            rows = con.execute(text(f"""
                SELECT concept_id, domain_id, standard_concept, invalid_reason
                FROM [{target_schema}].[concept]
                WHERE concept_id IN ({values})
            """)).fetchall()
            found = {int(r[0]): r for r in rows}
            invalid = []
            for label, cid in acute_concepts.items():
                r = found.get(cid)
                if r is None or r[1] != "Visit" or r[2] != "S" or r[3] is not None:
                    invalid.append({"source_enc_type": label, "concept_id": cid, "row": None if r is None else list(r)})
            if invalid:
                raise RuntimeError(f"Invalid frozen acute-care Visit concepts: {invalid}")
    finally:
        engine.dispose()

    d0_row = next((r for r in stage_c["phenotype_rows"] if r["phenotype"] == "D0"), None)
    if d0_row is None:
        raise RuntimeError("Stage C summary does not contain D0")

    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_d_analytical_equivalence"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "stage_d_stroke_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "inherited_d0_definition": d0["study_definition"],
        "inherited_d0_definition_sha256": _sha256(D0_PATH),
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "stage_c_d0_anchor": {
            "source_patients": d0_row["source_patients"],
            "lineage_faithful_omop_patients": d0_row["lineage_faithful_omop_patients"],
            "intersection_patients": d0_row["intersection_patients"],
            "exact_index_date_percent_among_shared": d0_row["exact_index_date_percent_among_shared"],
        },
        "acute_care_visit_concepts": acute_concepts,
        "required_column_checks": checks,
        "equivalence_margins": study["equivalence_margins"],
        "outcome_query_performed": False,
        "cross_cdm_outcome_query_performed": False,
        "guardrail": "Preflight validates locked definitions, completed Stage C anchors, schemas, and frozen acute-care visit mappings only. It does not count Stage D outcomes in either representation.",
    }
    out = out_dir / "stage_d_stroke_preflight.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_d_stroke_preflight_ready")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"study_definition_sha256: {payload['study_definition_sha256']}")
    print(f"inherited_d0_definition_sha256: {payload['inherited_d0_definition_sha256']}")
    print(f"analysis_git_sha: {payload['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {payload['analysis_worktree_clean']}")
    print(f"stage_c_d0_anchor: {payload['stage_c_d0_anchor']}")
    print(f"acute_care_visit_concepts: {acute_concepts}")
    print("outcome_query_performed: false")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Outcome-free preflight for Stage D stroke analytical equivalence")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
