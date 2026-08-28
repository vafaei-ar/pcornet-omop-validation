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
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_NAME = "stage-c-stroke-d0-v1"


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return "unknown"


def _schema(v: object) -> str:
    s = str(v or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe schema: {s!r}")
    return s


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sql_list(values: set[str] | frozenset[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _pct(n: int, d: int) -> float | None:
    return None if d == 0 else 100.0 * n / d


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    study_path = Path("study_definitions/stage_c_stroke_d0_v1.json")
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != STUDY_NAME or study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Locked Stage C D0 definition does not match expected study/frozen ETL SHA")

    out = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d0"
    out.mkdir(parents=True, exist_ok=True)

    all_codes = set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES)
    code_list = _sql_list(all_codes)
    icd9 = _sql_list(ICD9_STROKE_CODES)
    icd10 = _sql_list(ICD10_STROKE_CODES)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            for t in ("PCORnet_DIAGNOSIS", "PCORnet_ENCOUNTER", "PCORnet_DEMOGRAPHIC"):
                if not table_exists(con, source_schema, t):
                    raise RuntimeError(f"Missing [{source_schema}].[{t}]")
            for t in ("person", "visit_occurrence", "condition_occurrence", "concept", "concept_relationship", "etl_condition_occurrence_xwalk", "etl_visit_occurrence_xwalk"):
                if not table_exists(con, target_schema, t):
                    raise RuntimeError(f"Missing [{target_schema}].[{t}]")

            print("progress: materializing locked PCORnet D0 source cohort", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_d0') IS NOT NULL DROP TABLE #src_d0")
            con.exec_driver_sql(f"""
            ;WITH dx_rank AS (
              SELECT
                CONVERT(nvarchar(255), d.PATID) AS patid,
                CONVERT(nvarchar(255), d.ENCOUNTERID) AS encounterid,
                CONVERT(nvarchar(255), d.DIAGNOSISID) AS diagnosisid,
                CAST(d.DX_DATE AS date) AS dx_date,
                {_norm('d.DX')} AS dx_norm,
                ROW_NUMBER() OVER (
                  PARTITION BY CONVERT(nvarchar(255), d.PATID), CONVERT(nvarchar(255), d.ENCOUNTERID)
                  ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END, CAST(d.DX_DATE AS date), {_norm('d.DX')}, CONVERT(nvarchar(255), d.DIAGNOSISID)
                ) AS rn
              FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
              WHERE {_norm('d.DX')} IN ({code_list})
                AND {_short('d.PDX')}='P'
            ), encounters AS (
              SELECT
                x.patid, x.encounterid, x.diagnosisid, x.dx_date, x.dx_norm,
                CAST(e.ADMIT_DATE AS date) AS admit_date,
                CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                COALESCE(x.dx_date, CAST(e.ADMIT_DATE AS date), CAST(e.DISCHARGE_DATE AS date)) AS index_date,
                CAST(dm.BIRTH_DATE AS date) AS birth_date
              FROM dx_rank x
              JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                ON CONVERT(nvarchar(255), e.PATID)=x.patid
               AND CONVERT(nvarchar(255), e.ENCOUNTERID)=x.encounterid
              JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm
                ON CONVERT(nvarchar(255), dm.PATID)=x.patid
              WHERE x.rn=1
                AND {_short('e.ENC_TYPE')} IN ('EI','IP')
                AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
                AND DATEDIFF(day, CAST(e.ADMIT_DATE AS date), CAST(e.DISCHARGE_DATE AS date)) >= 1
            ), ranked AS (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY patid ORDER BY index_date, encounterid) AS patient_rn
              FROM encounters
              WHERE index_date IS NOT NULL
            )
            SELECT patid, encounterid, diagnosisid, dx_date, admit_date, discharge_date, index_date, birth_date,
                   CASE WHEN FLOOR(DATEDIFF(day,birth_date,index_date)/365.0) >= 18 THEN 1 ELSE 0 END AS adult_flag
            INTO #src_d0
            FROM ranked WHERE patient_rn=1;
            CREATE UNIQUE CLUSTERED INDEX IX_src_d0 ON #src_d0(patid);
            """)
            source_pre_age = _scalar(con, "SELECT COUNT_BIG(*) FROM #src_d0")
            source_patients = _scalar(con, "SELECT COUNT_BIG(*) FROM #src_d0 WHERE adult_flag=1")

            print("progress: materializing lineage-faithful frozen OMOP D0 cohort", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_faithful') IS NOT NULL DROP TABLE #omop_faithful")
            con.exec_driver_sql(f"""
            SELECT s.patid,
                   s.index_date AS source_index_date,
                   COALESCE(CAST(co.condition_start_date AS date), CAST(v.visit_start_date AS date), CAST(v.visit_end_date AS date)) AS omop_index_date,
                   v.visit_occurrence_id,
                   co.condition_occurrence_id
            INTO #omop_faithful
            FROM #src_d0 s
            JOIN [{target_schema}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=s.patid
            JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
            JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
            JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
              ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
            JOIN [{target_schema}].[condition_occurrence] co
              ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=p.person_id
            WHERE s.adult_flag=1
              AND v.visit_occurrence_id=co.visit_occurrence_id;
            CREATE INDEX IX_omop_faithful ON #omop_faithful(patid);
            """)

            # Deduplicate any one-to-many condition materialization to one patient/index row.
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_faithful_one') IS NOT NULL DROP TABLE #omop_faithful_one")
            con.exec_driver_sql("""
            ;WITH r AS (
              SELECT *, ROW_NUMBER() OVER(PARTITION BY patid ORDER BY omop_index_date, condition_occurrence_id) rn
              FROM #omop_faithful
            )
            SELECT patid, source_index_date, omop_index_date INTO #omop_faithful_one FROM r WHERE rn=1;
            CREATE UNIQUE CLUSTERED INDEX IX_omop_faithful_one ON #omop_faithful_one(patid);
            """)

            print("progress: resolving locked ICD codes to native OMOP Standard Condition concepts", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#stroke_standard') IS NOT NULL DROP TABLE #stroke_standard")
            con.exec_driver_sql(f"""
            SELECT DISTINCT cr.concept_id_2 AS concept_id
            INTO #stroke_standard
            FROM [{target_schema}].[concept] src
            JOIN [{target_schema}].[concept_relationship] cr
              ON cr.concept_id_1=src.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
            JOIN [{target_schema}].[concept] tgt
              ON tgt.concept_id=cr.concept_id_2
             AND tgt.standard_concept='S' AND tgt.invalid_reason IS NULL AND tgt.domain_id='Condition'
            WHERE ((src.vocabulary_id='ICD10CM' AND REPLACE(UPPER(src.concept_code),'.','') IN ({icd10}))
                OR (src.vocabulary_id='ICD9CM' AND REPLACE(UPPER(src.concept_code),'.','') IN ({icd9})))
              AND src.invalid_reason IS NULL;
            CREATE UNIQUE CLUSTERED INDEX IX_stroke_standard ON #stroke_standard(concept_id);
            """)

            print("progress: materializing secondary native-OMOP portable D0 sensitivity", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#native_d0') IS NOT NULL DROP TABLE #native_d0")
            con.exec_driver_sql(f"""
            ;WITH q AS (
              SELECT DISTINCT p.person_id, CONVERT(nvarchar(255),p.person_source_value) AS patid,
                     v.visit_occurrence_id, CAST(v.visit_start_date AS date) AS index_date, CAST(p.birth_datetime AS date) AS birth_date
              FROM [{target_schema}].[condition_occurrence] co
              JOIN #stroke_standard ss ON ss.concept_id=co.condition_concept_id
              JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=co.visit_occurrence_id
              JOIN [{target_schema}].[person] p ON p.person_id=co.person_id
              WHERE v.visit_concept_id IN (262,9201)
                AND v.visit_start_date IS NOT NULL AND v.visit_end_date IS NOT NULL
                AND DATEDIFF(day,CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) >= 1
                AND p.birth_datetime IS NOT NULL
            ), r AS (
              SELECT *, ROW_NUMBER() OVER(PARTITION BY person_id ORDER BY index_date,visit_occurrence_id) rn FROM q
            )
            SELECT patid,index_date
            INTO #native_d0
            FROM r
            WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0) >= 18;
            CREATE UNIQUE CLUSTERED INDEX IX_native_d0 ON #native_d0(patid);
            """)

            print("progress: computing cohort overlap and index-date agreement", flush=True)
            primary_row = con.execute(text("""
            WITH s AS (SELECT patid,index_date FROM #src_d0 WHERE adult_flag=1),
                 o AS (SELECT patid,omop_index_date FROM #omop_faithful_one),
                 u AS (SELECT patid FROM s UNION SELECT patid FROM o)
            SELECT
              (SELECT COUNT_BIG(*) FROM s) source_patients,
              (SELECT COUNT_BIG(*) FROM o) omop_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) intersection_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NULL THEN 1 ELSE 0 END) source_only_patients,
              SUM(CASE WHEN s.patid IS NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) omop_only_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND s.index_date=o.omop_index_date THEN 1 ELSE 0 END) exact_date_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND ABS(DATEDIFF(day,s.index_date,o.omop_index_date))<=1 THEN 1 ELSE 0 END) within1_date_patients
            FROM u LEFT JOIN s ON s.patid=u.patid LEFT JOIN o ON o.patid=u.patid
            """)).mappings().one()
            p = {k:int(v or 0) for k,v in dict(primary_row).items()}
            union = p["intersection_patients"] + p["source_only_patients"] + p["omop_only_patients"]
            primary = {
                **p,
                "union_patients": union,
                "patient_jaccard": None if union==0 else p["intersection_patients"]/union,
                "positive_agreement_percent": _pct(2*p["intersection_patients"], p["source_patients"]+p["omop_patients"]),
                "exact_index_date_percent_among_shared": _pct(p["exact_date_patients"],p["intersection_patients"]),
                "within_1_day_percent_among_shared": _pct(p["within1_date_patients"],p["intersection_patients"]),
            }

            native_row = con.execute(text("""
            WITH s AS (SELECT patid FROM #src_d0 WHERE adult_flag=1),
                 n AS (SELECT patid FROM #native_d0),
                 u AS (SELECT patid FROM s UNION SELECT patid FROM n)
            SELECT (SELECT COUNT_BIG(*) FROM s) source_patients,
                   (SELECT COUNT_BIG(*) FROM n) native_omop_patients,
                   SUM(CASE WHEN s.patid IS NOT NULL AND n.patid IS NOT NULL THEN 1 ELSE 0 END) intersection_patients,
                   SUM(CASE WHEN s.patid IS NOT NULL AND n.patid IS NULL THEN 1 ELSE 0 END) source_only_patients,
                   SUM(CASE WHEN s.patid IS NULL AND n.patid IS NOT NULL THEN 1 ELSE 0 END) native_only_patients
            FROM u LEFT JOIN s ON s.patid=u.patid LEFT JOIN n ON n.patid=u.patid
            """)).mappings().one()
            n = {k:int(v or 0) for k,v in dict(native_row).items()}
            n_union = n["intersection_patients"] + n["source_only_patients"] + n["native_only_patients"]
            native = {**n, "union_patients":n_union, "patient_jaccard":None if n_union==0 else n["intersection_patients"]/n_union,
                      "positive_agreement_percent":_pct(2*n["intersection_patients"], n["source_patients"]+n["native_omop_patients"])}

            print("progress: decomposing primary source-only discordance", flush=True)
            categories = [dict(r) for r in con.execute(text(f"""
            WITH s AS (
              SELECT * FROM #src_d0 WHERE adult_flag=1
            ), missing AS (
              SELECT s.patid,
                     CASE
                       WHEN s.dx_date IS NULL THEN 'required_source_date_missing_or_etl_excluded'
                       WHEN vx.visit_occurrence_id IS NULL THEN 'visit_not_materialized_or_unlinked'
                       WHEN cx.condition_occurrence_id IS NULL OR co.condition_occurrence_id IS NULL THEN 'stroke_diagnosis_not_materialized_or_unlinked'
                       ELSE 'unexpected_or_unexplained'
                     END AS category
              FROM s
              LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
              LEFT JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
              LEFT JOIN [{target_schema}].[condition_occurrence] co ON co.condition_occurrence_id=cx.condition_occurrence_id
              LEFT JOIN #omop_faithful_one o ON o.patid=s.patid
              WHERE o.patid IS NULL
            )
            SELECT category, COUNT_BIG(DISTINCT patid) AS patients
            FROM missing GROUP BY category ORDER BY category
            """)).mappings().all()]

            # Diagnostic only: recognized DX_TYPE effect on source candidate membership.
            dx_type_diag = _scalar(con, f"""
            ;WITH dx AS (
              SELECT DISTINCT CONVERT(nvarchar(255),d.PATID) patid
              FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
              WHERE {_norm('d.DX')} IN ({code_list}) AND {_short('d.PDX')}='P'
                AND {_short('d.DX_TYPE')} IN ('09','9','ICD9','ICD9CM','10','ICD10','ICD10CM')
            ) SELECT COUNT_BIG(*) FROM dx
            """)
    finally:
        engine.dispose()

    summary = {
        "status":"stage_c_stroke_d0_concordance_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "study_definition":STUDY_NAME,
        "study_definition_sha256":_sha(study_path),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "source_reference":{"first_qualifying_patients_before_age":source_pre_age,"adult_d0_patients":source_patients},
        "primary_transformation_fidelity":primary,
        "primary_source_only_discordance_categories":categories,
        "secondary_native_omop_portability":native,
        "diagnostic_only":{"distinct_patients_with_locked_stroke_primary_dx_and_recognized_dx_type":dx_type_diag,
                           "note":"DX_TYPE is diagnostic only and does not define the primary D0 cohort."},
        "interpretation_rules":[
            "Primary D0 exactly follows the locked source-reference ordering, overnight, PDX, and post-index age rules.",
            "PDX and exact source diagnosis identity are evaluated through frozen lineage because PDX is not natively represented in OMOP core.",
            "The native-OMOP portable sensitivity uses only Standard Condition concepts and frozen EI/IP visit concepts and intentionally omits PDX.",
            "Patient-level rows are not written; outputs are aggregate only.",
        ],
    }
    path = out / "stage_c_stroke_d0_concordance.json"
    path.write_text(json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8")
    print("status: stage_c_stroke_d0_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_d0_patients: {primary['source_patients']}")
    print(f"lineage_faithful_omop_patients: {primary['omop_patients']}")
    print(f"intersection_patients: {primary['intersection_patients']}")
    print(f"source_only_patients: {primary['source_only_patients']}")
    print(f"omop_only_patients: {primary['omop_only_patients']}")
    print(f"patient_jaccard: {primary['patient_jaccard']}")
    print(f"exact_index_date_patients: {primary['exact_date_patients']}")
    print(f"native_omop_portable_patients: {native['native_omop_patients']}")
    print(f"output: {path}")
    return summary


def main() -> None:
    p=argparse.ArgumentParser(description="Locked Stage C ischemic-stroke D0 phenotype reproducibility")
    p.add_argument("--config",required=True)
    p.add_argument("--output-dir")
    a=p.parse_args(); run(a.config,a.output_dir)


if __name__=="__main__":
    main()
