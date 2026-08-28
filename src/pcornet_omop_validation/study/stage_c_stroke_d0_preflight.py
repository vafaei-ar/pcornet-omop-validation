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
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_c_stroke_d0_v1.json")


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


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _sql_values(code_system: str, values: set[str] | frozenset[str]) -> list[str]:
    out: list[str] = []
    for value in sorted(values):
        safe = value.replace("'", "''")
        out.append(f"('{code_system}','{safe}')")
    return out


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    study_path = Path(STUDY_DEFINITION)
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-c-stroke-d0-v1":
        raise RuntimeError("Stage C D0 preflight requires stage-c-stroke-d0-v1")
    if study.get("status") != "prespecified_before_stage_c_outcome_queries":
        raise RuntimeError("Stage C D0 definition is not in prespecified status")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage C D0 frozen ETL SHA mismatch")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent
        / "publication_analysis"
        / "stage_c_phenotypes"
        / "stroke_d0"
    )
    out.mkdir(parents=True, exist_ok=True)

    required_tables = [
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_ENCOUNTER"),
        (source_schema, "PCORnet_DEMOGRAPHIC"),
        (target_schema, "person"),
        (target_schema, "visit_occurrence"),
        (target_schema, "condition_occurrence"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "etl_condition_occurrence_xwalk"),
    ]

    required_columns = {
        (source_schema, "PCORnet_DIAGNOSIS"): {"PATID", "ENCOUNTERID", "DIAGNOSISID", "DX", "PDX", "DX_DATE"},
        (source_schema, "PCORnet_ENCOUNTER"): {"PATID", "ENCOUNTERID", "ENC_TYPE", "ADMIT_DATE", "DISCHARGE_DATE"},
        (source_schema, "PCORnet_DEMOGRAPHIC"): {"PATID", "BIRTH_DATE"},
        (target_schema, "person"): {"PERSON_ID", "PERSON_SOURCE_VALUE"},
        (target_schema, "visit_occurrence"): {"VISIT_OCCURRENCE_ID", "PERSON_ID", "VISIT_CONCEPT_ID", "VISIT_START_DATE", "VISIT_END_DATE"},
        (target_schema, "condition_occurrence"): {"CONDITION_OCCURRENCE_ID", "PERSON_ID", "CONDITION_CONCEPT_ID", "CONDITION_START_DATE", "VISIT_OCCURRENCE_ID"},
    }

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_tables = [
                f"[{schema}].[{table}]"
                for schema, table in required_tables
                if not table_exists(con, schema, table)
            ]
            if missing_tables:
                raise RuntimeError(f"Missing Stage C D0 required tables: {missing_tables}")

            column_checks: list[dict[str, Any]] = []
            for key, expected in required_columns.items():
                schema, table = key
                actual = _columns(con, schema, table)
                missing = sorted(expected - actual)
                column_checks.append({
                    "schema": schema,
                    "table": table,
                    "missing_required_columns": missing,
                })
                if missing:
                    raise RuntimeError(f"[{schema}].[{table}] missing columns: {missing}")

            person_cols = _columns(con, target_schema, "person")
            condition_cols = _columns(con, target_schema, "condition_occurrence")
            person_birth_available = any(
                c in person_cols for c in ("BIRTH_DATETIME", "YEAR_OF_BIRTH")
            )
            if not person_birth_available:
                raise RuntimeError("OMOP person lacks birth representation required for D0 age rule")

            native_pdx_like_columns = sorted(
                c for c in condition_cols if c in {"PDX", "PRIMARY_DIAGNOSIS", "PRIMARY_DIAGNOSIS_FLAG"}
            )

            source_duplicate_patid_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) AS patid
                  FROM [{source_schema}].[PCORnet_DEMOGRAPHIC]
                  GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), PATID)))
                  HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
            target_duplicate_person_source_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT person_source_value
                  FROM [{target_schema}].[person]
                  WHERE person_source_value IS NOT NULL
                  GROUP BY person_source_value
                  HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
            if source_duplicate_patid_groups or target_duplicate_person_source_groups:
                raise RuntimeError(
                    "Stage C patient bridge is not unique: "
                    f"source duplicate PATID groups={source_duplicate_patid_groups}, "
                    f"target duplicate person_source_value groups={target_duplicate_person_source_groups}"
                )

            source_patids = _scalar(
                con,
                f"SELECT COUNT_BIG(DISTINCT LTRIM(RTRIM(CONVERT(nvarchar(255), PATID)))) "
                f"FROM [{source_schema}].[PCORnet_DEMOGRAPHIC] WHERE PATID IS NOT NULL",
            )
            linked_patids = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(DISTINCT LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))))
                FROM [{source_schema}].[PCORnet_DEMOGRAPHIC] d
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                WHERE d.PATID IS NOT NULL
                """,
            )

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#stage_c_stroke_codes') IS NOT NULL DROP TABLE #stage_c_stroke_codes")
            values_sql = ",\n".join(
                _sql_values("ICD9CM", ICD9_STROKE_CODES)
                + _sql_values("ICD10CM", ICD10_STROKE_CODES)
            )
            con.exec_driver_sql(
                f"""
                CREATE TABLE #stage_c_stroke_codes (
                  vocabulary_id varchar(20) NOT NULL,
                  normalized_code varchar(32) NOT NULL
                );
                INSERT INTO #stage_c_stroke_codes(vocabulary_id, normalized_code)
                VALUES {values_sql};
                CREATE UNIQUE CLUSTERED INDEX IX_stage_c_stroke_codes
                  ON #stage_c_stroke_codes(vocabulary_id, normalized_code);
                """
            )

            code_resolution = [
                dict(r)
                for r in con.execute(
                    text(
                        f"""
                        WITH source_concepts AS (
                          SELECT sc.vocabulary_id, sc.normalized_code, c.concept_id,
                                 c.standard_concept, c.domain_id
                          FROM #stage_c_stroke_codes sc
                          LEFT JOIN [{target_schema}].[concept] c
                            ON c.vocabulary_id = sc.vocabulary_id
                           AND REPLACE(UPPER(LTRIM(RTRIM(c.concept_code))),'.','') = sc.normalized_code
                           AND c.invalid_reason IS NULL
                        ), targets AS (
                          SELECT s.vocabulary_id, s.normalized_code, s.concept_id AS source_concept_id,
                                 CASE WHEN s.standard_concept='S' AND s.domain_id='Condition' THEN s.concept_id END AS direct_target_id,
                                 CASE WHEN cr.relationship_id='Maps to'
                                           AND tc.standard_concept='S'
                                           AND tc.invalid_reason IS NULL
                                           AND tc.domain_id='Condition'
                                      THEN tc.concept_id END AS maps_to_target_id
                          FROM source_concepts s
                          LEFT JOIN [{target_schema}].[concept_relationship] cr
                            ON cr.concept_id_1=s.concept_id
                           AND cr.relationship_id='Maps to'
                           AND cr.invalid_reason IS NULL
                          LEFT JOIN [{target_schema}].[concept] tc
                            ON tc.concept_id=cr.concept_id_2
                        )
                        SELECT vocabulary_id,
                               COUNT_BIG(DISTINCT normalized_code) AS locked_codes,
                               COUNT_BIG(DISTINCT CASE WHEN source_concept_id IS NOT NULL THEN normalized_code END) AS codes_with_active_source_concept,
                               COUNT_BIG(DISTINCT CASE WHEN COALESCE(direct_target_id,maps_to_target_id) IS NOT NULL THEN normalized_code END) AS codes_with_standard_condition_target,
                               COUNT_BIG(DISTINCT COALESCE(direct_target_id,maps_to_target_id)) AS distinct_standard_condition_targets
                        FROM targets
                        GROUP BY vocabulary_id
                        ORDER BY vocabulary_id
                        """
                    )
                ).mappings().all()
            ]
            for row in code_resolution:
                for key in (
                    "locked_codes",
                    "codes_with_active_source_concept",
                    "codes_with_standard_condition_target",
                    "distinct_standard_condition_targets",
                ):
                    row[key] = int(row[key] or 0)

            visit_concepts = [
                dict(r)
                for r in con.execute(
                    text(
                        f"""
                        SELECT concept_id, concept_name, domain_id, standard_concept, invalid_reason
                        FROM [{target_schema}].[concept]
                        WHERE concept_id IN (262, 9201)
                        ORDER BY concept_id
                        """
                    )
                ).mappings().all()
            ]
            valid_visit_ids = {
                int(r["concept_id"])
                for r in visit_concepts
                if r["domain_id"] == "Visit"
                and r["standard_concept"] == "S"
                and r["invalid_reason"] is None
            }
            if valid_visit_ids != {262, 9201}:
                raise RuntimeError(f"Frozen EI/IP Standard Visit concepts failed validation: {visit_concepts}")

    finally:
        engine.dispose()

    stroke_codes_path = Path(__file__).with_name("stroke_codes.py")
    summary: dict[str, Any] = {
        "status": "stage_c_stroke_d0_preflight_ready",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(study_path),
        "stroke_codes_sha256": _sha256(stroke_codes_path),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "patient_bridge": {
            "status": "matched_unique_source_bridge" if source_patids == linked_patids else "incomplete_source_bridge",
            "source_distinct_patids": source_patids,
            "linked_distinct_patids": linked_patids,
            "source_duplicate_patid_groups": source_duplicate_patid_groups,
            "target_duplicate_person_source_value_groups": target_duplicate_person_source_groups,
        },
        "native_omop_representability": {
            "birth_representation_available": person_birth_available,
            "pdx_like_core_columns": native_pdx_like_columns,
            "pdx_natively_representable": bool(native_pdx_like_columns),
            "primary_design_consequence": "PDX is evaluated through frozen lineage for the primary transformation-fidelity D0 and omitted from the secondary native-portable D0 sensitivity when no native PDX-like column exists.",
        },
        "locked_code_counts": {
            "icd9": len(ICD9_STROKE_CODES),
            "icd10": len(ICD10_STROKE_CODES),
            "total": len(ICD9_STROKE_CODES) + len(ICD10_STROKE_CODES),
        },
        "vocabulary_resolution": code_resolution,
        "native_portable_visit_concepts": visit_concepts,
        "required_column_checks": column_checks,
        "outcome_query_performed": False,
        "note": "This preflight validates representability, vocabulary resolution, lineage prerequisites, and patient linkage only. It does not compute D0 cohort membership or concordance outcomes.",
    }

    out_json = out / "stage_c_stroke_d0_preflight.json"
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")

    print("status: stage_c_stroke_d0_preflight_ready")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"study_definition: {summary['study_definition']}")
    print(f"study_definition_sha256: {summary['study_definition_sha256']}")
    print(f"stroke_codes_sha256: {summary['stroke_codes_sha256']}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"patient_bridge_status: {summary['patient_bridge']['status']}")
    print(f"native_pdx_representable: {summary['native_omop_representability']['pdx_natively_representable']}")
    for row in code_resolution:
        print(
            "vocabulary_resolution: "
            f"{row['vocabulary_id']} locked={row['locked_codes']} "
            f"source_concept={row['codes_with_active_source_concept']} "
            f"standard_condition_target={row['codes_with_standard_condition_target']} "
            f"distinct_targets={row['distinct_standard_condition_targets']}"
        )
    print(f"output: {out_json}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage C stroke D0 prespecified preflight")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
