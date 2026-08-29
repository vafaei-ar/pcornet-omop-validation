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
STUDY_DEFINITION = Path("study_definitions/stage_c_stroke_d1_d3_v1.json")
LIPID_ARTIFACT = Path("study_definitions/artifacts/stage_c_lipid_loinc_whitelist_v1.csv")
LIPID_PROVENANCE = Path("study_definitions/artifacts/stage_c_lipid_loinc_whitelist_v1.provenance.json")
CT_CODES = frozenset({"70450", "70460", "70470"})
MRI_CODES = frozenset({"70551", "70552", "70553", "70557", "70558", "70559"})
CPT_TYPES = frozenset({"CH", "CPT", "CPT4", "HCPCS"})
LAB_DATE_PRIORITY = ("LAB_TKN_DTTM", "SPECIMEN_DATE", "LAB_DATE", "RESULT_DATE")


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


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table"
        ),
        {"schema": schema, "table": table},
    ).fetchall()
    return {str(r[0]).upper() for r in rows}


def _load_lipid_loincs(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "LOINC_NUM" not in reader.fieldnames:
            raise RuntimeError("Versioned lipid artifact must contain LOINC_NUM")
        values = sorted({str(r["LOINC_NUM"]).strip().upper() for r in reader if str(r.get("LOINC_NUM") or "").strip()})
    if not values:
        raise RuntimeError("Versioned lipid LOINC artifact is empty")
    return values


def _insert_temp_values(con, table: str, vocabulary: str, codes: list[str]) -> None:
    con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#{table}') IS NOT NULL DROP TABLE #{table}")
    con.exec_driver_sql(
        f"CREATE TABLE #{table} (vocabulary_id varchar(20) NOT NULL, normalized_code varchar(64) NOT NULL)"
    )
    params = [(vocabulary, code) for code in codes]
    con.exec_driver_sql(
        f"INSERT INTO #{table}(vocabulary_id, normalized_code) VALUES (?, ?)", params
    )
    con.exec_driver_sql(
        f"CREATE UNIQUE CLUSTERED INDEX IX_{table} ON #{table}(vocabulary_id, normalized_code)"
    )


def _resolution(con, target_schema: str, temp_table: str, allowed_domains: tuple[str, ...]) -> dict[str, Any]:
    domain_list = ",".join("'" + d.replace("'", "''") + "'" for d in allowed_domains)
    row = con.execute(
        text(
            f"""
            WITH source_concepts AS (
              SELECT sc.normalized_code, c.concept_id, c.standard_concept, c.domain_id
              FROM #{temp_table} sc
              LEFT JOIN [{target_schema}].[concept] c
                ON c.vocabulary_id=sc.vocabulary_id
               AND REPLACE(UPPER(LTRIM(RTRIM(c.concept_code))),'.','')=sc.normalized_code
               AND c.invalid_reason IS NULL
            ), targets AS (
              SELECT s.normalized_code,
                     s.concept_id AS source_concept_id,
                     CASE WHEN s.standard_concept='S' AND s.domain_id IN ({domain_list}) THEN s.concept_id END AS direct_target_id,
                     CASE WHEN cr.relationship_id='Maps to'
                               AND tc.standard_concept='S'
                               AND tc.invalid_reason IS NULL
                               AND tc.domain_id IN ({domain_list})
                          THEN tc.concept_id END AS maps_to_target_id,
                     COALESCE(
                       CASE WHEN s.standard_concept='S' AND s.domain_id IN ({domain_list}) THEN s.domain_id END,
                       CASE WHEN cr.relationship_id='Maps to' AND tc.standard_concept='S' AND tc.invalid_reason IS NULL AND tc.domain_id IN ({domain_list}) THEN tc.domain_id END
                     ) AS target_domain
              FROM source_concepts s
              LEFT JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1=s.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
              LEFT JOIN [{target_schema}].[concept] tc ON tc.concept_id=cr.concept_id_2
            )
            SELECT COUNT_BIG(DISTINCT normalized_code) AS locked_codes,
                   COUNT_BIG(DISTINCT CASE WHEN source_concept_id IS NOT NULL THEN normalized_code END) AS codes_with_active_source_concept,
                   COUNT_BIG(DISTINCT CASE WHEN COALESCE(direct_target_id,maps_to_target_id) IS NOT NULL THEN normalized_code END) AS codes_with_standard_target,
                   COUNT_BIG(DISTINCT COALESCE(direct_target_id,maps_to_target_id)) AS distinct_standard_targets
            FROM targets
            """
        )
    ).mappings().one()
    out = {k: int(v or 0) for k, v in dict(row).items()}
    by_domain = [
        {"target_domain": str(r[0]), "distinct_targets": int(r[1] or 0), "codes": int(r[2] or 0)}
        for r in con.execute(
            text(
                f"""
                WITH source_concepts AS (
                  SELECT sc.normalized_code, c.concept_id, c.standard_concept, c.domain_id
                  FROM #{temp_table} sc
                  JOIN [{target_schema}].[concept] c
                    ON c.vocabulary_id=sc.vocabulary_id
                   AND REPLACE(UPPER(LTRIM(RTRIM(c.concept_code))),'.','')=sc.normalized_code
                   AND c.invalid_reason IS NULL
                ), mapped AS (
                  SELECT s.normalized_code,
                         COALESCE(
                           CASE WHEN s.standard_concept='S' AND s.domain_id IN ({domain_list}) THEN s.concept_id END,
                           CASE WHEN cr.relationship_id='Maps to' AND tc.standard_concept='S' AND tc.invalid_reason IS NULL AND tc.domain_id IN ({domain_list}) THEN tc.concept_id END
                         ) AS target_id,
                         COALESCE(
                           CASE WHEN s.standard_concept='S' AND s.domain_id IN ({domain_list}) THEN s.domain_id END,
                           CASE WHEN cr.relationship_id='Maps to' AND tc.standard_concept='S' AND tc.invalid_reason IS NULL AND tc.domain_id IN ({domain_list}) THEN tc.domain_id END
                         ) AS target_domain
                  FROM source_concepts s
                  LEFT JOIN [{target_schema}].[concept_relationship] cr
                    ON cr.concept_id_1=s.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
                  LEFT JOIN [{target_schema}].[concept] tc ON tc.concept_id=cr.concept_id_2
                )
                SELECT target_domain, COUNT_BIG(DISTINCT target_id), COUNT_BIG(DISTINCT normalized_code)
                FROM mapped WHERE target_id IS NOT NULL
                GROUP BY target_domain ORDER BY target_domain
                """
            )
        ).fetchall()
    ]
    out["by_target_domain"] = by_domain
    return out


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    study = json.loads(STUDY_DEFINITION.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-c-stroke-d1-d3-v1":
        raise RuntimeError("Stage C D1/D3 preflight requires stage-c-stroke-d1-d3-v1")
    if study.get("status") != "prespecified_before_stage_c_d1_d3_outcome_queries":
        raise RuntimeError("Stage C D1/D3 definition is not in prespecified status")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage C D1/D3 frozen ETL SHA mismatch")
    if not LIPID_ARTIFACT.exists() or not LIPID_PROVENANCE.exists():
        raise RuntimeError("Versioned D1/D3 lipid artifact/provenance is missing")

    lipid_loincs = _load_lipid_loincs(LIPID_ARTIFACT)
    provenance = json.loads(LIPID_PROVENANCE.read_text(encoding="utf-8"))

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    out = Path(output_dir) if output_dir else config.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d1_d3"
    out.mkdir(parents=True, exist_ok=True)

    required_tables = [
        (source_schema, "PCORnet_DIAGNOSIS"), (source_schema, "PCORnet_ENCOUNTER"),
        (source_schema, "PCORnet_DEMOGRAPHIC"), (source_schema, "PCORnet_PROCEDURES"),
        (source_schema, "PCORnet_LAB_RESULT_CM"),
        (target_schema, "person"), (target_schema, "visit_occurrence"),
        (target_schema, "condition_occurrence"), (target_schema, "procedure_occurrence"),
        (target_schema, "measurement"), (target_schema, "observation"),
        (target_schema, "concept"), (target_schema, "concept_relationship"),
        (target_schema, "etl_visit_occurrence_xwalk"), (target_schema, "etl_condition_occurrence_xwalk"),
        (target_schema, "etl_procedure_occurrence_xwalk"), (target_schema, "etl_measurement_xwalk"),
        (target_schema, "etl_observation_xwalk"),
    ]
    required_columns = {
        (source_schema, "PCORnet_PROCEDURES"): {"PATID", "PROCEDURESID", "PX", "PX_TYPE", "PX_DATE"},
        (source_schema, "PCORnet_LAB_RESULT_CM"): {"PATID", "LAB_RESULT_CM_ID", "LAB_LOINC"},
        (target_schema, "procedure_occurrence"): {"PROCEDURE_OCCURRENCE_ID", "PERSON_ID", "PROCEDURE_CONCEPT_ID", "PROCEDURE_DATE"},
        (target_schema, "measurement"): {"MEASUREMENT_ID", "PERSON_ID", "MEASUREMENT_CONCEPT_ID", "MEASUREMENT_DATE"},
        (target_schema, "observation"): {"OBSERVATION_ID", "PERSON_ID", "OBSERVATION_CONCEPT_ID", "OBSERVATION_DATE"},
    }

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing = [f"[{s}].[{t}]" for s, t in required_tables if not table_exists(con, s, t)]
            if missing:
                raise RuntimeError(f"Missing Stage C D1/D3 required tables: {missing}")

            column_checks: list[dict[str, Any]] = []
            for (schema, table), expected in required_columns.items():
                actual = _columns(con, schema, table)
                miss = sorted(expected - actual)
                column_checks.append({"schema": schema, "table": table, "missing_required_columns": miss})
                if miss:
                    raise RuntimeError(f"[{schema}].[{table}] missing columns: {miss}")

            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            available_dates = [c for c in LAB_DATE_PRIORITY if c in lab_cols]
            if not available_dates:
                raise RuntimeError("LAB_RESULT_CM has none of the prespecified D1/D3 lipid date fields")
            selected_lab_date = available_dates[0]

            _insert_temp_values(con, "stage_c_imaging_codes", "CPT4", sorted(CT_CODES | MRI_CODES))
            imaging_resolution = _resolution(con, target_schema, "stage_c_imaging_codes", ("Procedure", "Measurement", "Observation"))
            _insert_temp_values(con, "stage_c_lipid_loincs", "LOINC", lipid_loincs)
            lipid_resolution = _resolution(con, target_schema, "stage_c_lipid_loincs", ("Measurement", "Observation"))

            if imaging_resolution["codes_with_active_source_concept"] != len(CT_CODES | MRI_CODES):
                raise RuntimeError(f"Not all locked imaging CPT codes resolve to active source concepts: {imaging_resolution}")
            if imaging_resolution["codes_with_standard_target"] != len(CT_CODES | MRI_CODES):
                raise RuntimeError(f"Not all locked imaging CPT codes resolve to allowed active Standard targets: {imaging_resolution}")
            if lipid_resolution["codes_with_active_source_concept"] != len(lipid_loincs):
                raise RuntimeError(f"Not all locked lipid LOINCs resolve to active source concepts: {lipid_resolution}")
            if lipid_resolution["codes_with_standard_target"] != len(lipid_loincs):
                raise RuntimeError(f"Not all locked lipid LOINCs resolve to Measurement/Observation Standard targets: {lipid_resolution}")
    finally:
        engine.dispose()

    summary: dict[str, Any] = {
        "status": "stage_c_stroke_d1_d3_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_DEFINITION),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "lipid_artifact": {
            "path": str(LIPID_ARTIFACT),
            "sha256": _sha256(LIPID_ARTIFACT),
            "rows": len(lipid_loincs),
            "provenance_path": str(LIPID_PROVENANCE),
            "provenance_sha256": _sha256(LIPID_PROVENANCE),
            "upstream_blob_sha": provenance.get("upstream_git_blob_sha"),
        },
        "source_evidence_rules": {
            "ct_codes": sorted(CT_CODES), "mri_codes": sorted(MRI_CODES),
            "accepted_procedure_code_types": sorted(CPT_TYPES),
            "imaging_window": "ADMIT_DATE - 2 days through DISCHARGE_DATE inclusive",
            "lipid_date_field_priority": list(LAB_DATE_PRIORITY),
            "selected_lipid_date_field": selected_lab_date,
            "available_lipid_date_fields": available_dates,
            "lipid_window": "ADMIT_DATE through DISCHARGE_DATE inclusive",
        },
        "vocabulary_resolution": {"imaging_cpt": imaging_resolution, "lipid_loinc": lipid_resolution},
        "required_column_checks": column_checks,
        "outcome_query_performed": False,
        "note": "Preflight only: validates locked evidence artifacts, source/target prerequisites, selected source date field, and frozen vocabulary representability. It does not compute D1/D3 cohort membership or concordance outcomes.",
    }
    out_json = out / "stage_c_stroke_d1_d3_preflight.json"
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")

    print("status: stage_c_stroke_d1_d3_preflight_ready")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"study_definition_sha256: {summary['study_definition_sha256']}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"lipid_loinc_rows: {len(lipid_loincs)}")
    print(f"lipid_artifact_sha256: {summary['lipid_artifact']['sha256']}")
    print(f"selected_lipid_date_field: {selected_lab_date}")
    print(f"imaging_resolution: {imaging_resolution}")
    print(f"lipid_resolution: {lipid_resolution}")
    print(f"output: {out_json}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage C stroke D1/D3 prespecified preflight")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
