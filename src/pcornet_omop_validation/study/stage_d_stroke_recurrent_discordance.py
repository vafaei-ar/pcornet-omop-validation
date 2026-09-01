from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")


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


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage D definition is not anchored to frozen ETL")

    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_d_analytical_equivalence"
    stage_d_path = out_dir / "stage_d_stroke_analytical_equivalence.json"
    if not stage_d_path.exists():
        raise RuntimeError("Completed Stage D analytical-equivalence JSON is required")
    stage_d = json.loads(stage_d_path.read_text(encoding="utf-8"))
    if stage_d.get("status") != "stage_d_stroke_analytical_equivalence_complete":
        raise RuntimeError("Stage D analytical equivalence is not complete")
    if stage_d.get("study_definition_sha256") != _sha256(STUDY_PATH):
        raise RuntimeError("Stage D study definition differs from completed run")
    if stage_d.get("inherited_d0_definition_sha256") != _sha256(D0_PATH):
        raise RuntimeError("Inherited D0 definition differs from completed run")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    code_list = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    acute_source = "'ED','EI','IP'"
    acute_target = "9203,262,9201"

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            print("progress: reproducing locked source D0", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_d0') IS NOT NULL DROP TABLE #src_d0")
            con.exec_driver_sql(f"""
            ;WITH dx_rank AS (
              SELECT CONVERT(nvarchar(255),d.PATID) patid,
                     CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
                     CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,
                     CAST(d.DX_DATE AS date) dx_date,
                     ROW_NUMBER() OVER (
                       PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                       ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                     ) rn
              FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
              WHERE {_norm('d.DX')} IN ({code_list}) AND {_short('d.PDX')}='P'
            ), enc AS (
              SELECT x.patid,x.encounterid,x.diagnosisid,
                     CAST(e.ADMIT_DATE AS date) admit_date,CAST(e.DISCHARGE_DATE AS date) discharge_date,
                     COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) index_date,
                     CAST(dm.BIRTH_DATE AS date) birth_date
              FROM dx_rank x
              JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                ON CONVERT(nvarchar(255),e.PATID)=x.patid AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
              JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm ON CONVERT(nvarchar(255),dm.PATID)=x.patid
              WHERE x.rn=1 AND {_short('e.ENC_TYPE')} IN ('EI','IP')
                AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
                AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1
            ), ranked AS (
              SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY index_date,encounterid) patient_rn
              FROM enc WHERE index_date IS NOT NULL
            )
            SELECT patid,encounterid,diagnosisid,admit_date,discharge_date,index_date,birth_date
            INTO #src_d0 FROM ranked
            WHERE patient_rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
            CREATE UNIQUE CLUSTERED INDEX IX_recur_src_d0 ON #src_d0(patid);
            """)

            print("progress: reproducing lineage-faithful OMOP D0", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_d0_all') IS NOT NULL DROP TABLE #omop_d0_all")
            con.exec_driver_sql(f"""
            SELECT s.patid,p.person_id,s.encounterid,
                   CAST(v.visit_end_date AS date) omop_discharge_date,
                   COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) omop_index_date,
                   co.condition_occurrence_id
            INTO #omop_d0_all
            FROM #src_d0 s
            JOIN [{target_schema}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=s.patid
            JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
            JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
            JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
              ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
            JOIN [{target_schema}].[condition_occurrence] co
              ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=p.person_id AND co.visit_occurrence_id=v.visit_occurrence_id;
            """)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_d0') IS NOT NULL DROP TABLE #omop_d0")
            con.exec_driver_sql("""
            ;WITH r AS (
              SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY omop_index_date,condition_occurrence_id) rn FROM #omop_d0_all
            )
            SELECT patid,person_id,encounterid,omop_discharge_date,omop_index_date
            INTO #omop_d0 FROM r WHERE rn=1;
            CREATE UNIQUE CLUSTERED INDEX IX_recur_omop_d0 ON #omop_d0(patid);
            """)

            print("progress: materializing fixed-index 365-day eligible cohort", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fixed365') IS NOT NULL DROP TABLE #fixed365")
            con.exec_driver_sql(f"""
            SELECT s.patid,s.discharge_date,o.person_id,o.omop_discharge_date,o.omop_index_date
            INTO #fixed365
            FROM #src_d0 s
            JOIN #omop_d0 o ON o.patid=s.patid AND o.omop_index_date=s.index_date
            WHERE EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                WHERE CONVERT(nvarchar(255),en.PATID)=s.patid
                  AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                  AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,365,s.discharge_date)
            )
              AND EXISTS (
                SELECT 1 FROM [{target_schema}].[observation_period] op
                WHERE op.person_id=o.person_id
                  AND op.observation_period_start_date<=o.omop_discharge_date
                  AND op.observation_period_end_date>=DATEADD(day,365,o.omop_discharge_date)
            );
            CREATE UNIQUE CLUSTERED INDEX IX_recur_fixed365 ON #fixed365(patid);
            """)

            print("progress: materializing source recurrent-stroke candidates", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_candidates') IS NOT NULL DROP TABLE #src_candidates")
            con.exec_driver_sql(f"""
            SELECT f.patid,f.person_id,f.discharge_date,f.omop_discharge_date,
                   CONVERT(nvarchar(255),e.ENCOUNTERID) encounterid,
                   CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,
                   CAST(e.ADMIT_DATE AS date) source_event_date,
                   DATEDIFF(day,f.discharge_date,CAST(e.ADMIT_DATE AS date)) source_day,
                   {_short('e.ENC_TYPE')} source_enc_type,
                   {_norm('d.DX')} source_dx
            INTO #src_candidates
            FROM #fixed365 f
            JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
              ON CONVERT(nvarchar(255),e.PATID)=f.patid
            JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d
              ON CONVERT(nvarchar(255),d.PATID)=CONVERT(nvarchar(255),e.PATID)
             AND CONVERT(nvarchar(255),d.ENCOUNTERID)=CONVERT(nvarchar(255),e.ENCOUNTERID)
            WHERE CAST(e.ADMIT_DATE AS date)>=DATEADD(day,31,f.discharge_date)
              AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,365,f.discharge_date)
              AND {_short('e.ENC_TYPE')} IN ({acute_source})
              AND {_norm('d.DX')} IN ({code_list});
            CREATE INDEX IX_recur_src_candidates ON #src_candidates(patid,encounterid,diagnosisid);
            """)

            print("progress: auditing lineage and OMOP qualification", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#candidate_audit') IS NOT NULL DROP TABLE #candidate_audit")
            con.exec_driver_sql(f"""
            SELECT c.*,
                   vx.visit_occurrence_id,
                   CASE WHEN vx.visit_occurrence_id IS NOT NULL THEN 1 ELSE 0 END has_visit_xwalk,
                   CASE WHEN v.visit_occurrence_id IS NOT NULL THEN 1 ELSE 0 END has_visit,
                   v.visit_concept_id,
                   CAST(v.visit_start_date AS date) omop_visit_date,
                   CASE WHEN v.visit_occurrence_id IS NOT NULL AND v.visit_concept_id IN ({acute_target}) THEN 1 ELSE 0 END omop_visit_acute,
                   CASE WHEN v.visit_occurrence_id IS NOT NULL AND CAST(v.visit_start_date AS date)>=DATEADD(day,31,c.omop_discharge_date)
                              AND CAST(v.visit_start_date AS date)<=DATEADD(day,365,c.omop_discharge_date) THEN 1 ELSE 0 END omop_visit_in_window,
                   cx.condition_occurrence_id,
                   CASE WHEN cx.condition_occurrence_id IS NOT NULL THEN 1 ELSE 0 END has_condition_xwalk,
                   CASE WHEN co.condition_occurrence_id IS NOT NULL THEN 1 ELSE 0 END has_linked_condition
            INTO #candidate_audit
            FROM #src_candidates c
            LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
              ON CONVERT(nvarchar(255),vx.encounterid)=c.encounterid
            LEFT JOIN [{target_schema}].[visit_occurrence] v
              ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=c.person_id
            LEFT JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
              ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=c.diagnosisid
            LEFT JOIN [{target_schema}].[condition_occurrence] co
              ON co.condition_occurrence_id=cx.condition_occurrence_id
             AND co.person_id=c.person_id
             AND co.visit_occurrence_id=v.visit_occurrence_id;
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#patient_labels') IS NOT NULL DROP TABLE #patient_labels")
            con.exec_driver_sql("""
            SELECT f.patid,
                   CASE WHEN EXISTS (SELECT 1 FROM #src_candidates s WHERE s.patid=f.patid) THEN 1 ELSE 0 END source_positive,
                   CASE WHEN EXISTS (
                     SELECT 1 FROM #candidate_audit a
                     WHERE a.patid=f.patid AND a.has_visit=1 AND a.omop_visit_acute=1
                       AND a.omop_visit_in_window=1 AND a.has_linked_condition=1
                   ) THEN 1 ELSE 0 END omop_positive
            INTO #patient_labels
            FROM #fixed365 f;
            CREATE UNIQUE CLUSTERED INDEX IX_recur_patient_labels ON #patient_labels(patid);
            """)

            labels = con.execute(text("""
            SELECT COUNT_BIG(*) eligible,
                   SUM(CASE WHEN source_positive=1 THEN 1 ELSE 0 END) source_events,
                   SUM(CASE WHEN omop_positive=1 THEN 1 ELSE 0 END) omop_events,
                   SUM(CASE WHEN source_positive=omop_positive THEN 1 ELSE 0 END) label_agreement,
                   SUM(CASE WHEN source_positive=1 AND omop_positive=0 THEN 1 ELSE 0 END) source_only_positive,
                   SUM(CASE WHEN source_positive=0 AND omop_positive=1 THEN 1 ELSE 0 END) omop_only_positive
            FROM #patient_labels
            """)).mappings().one()
            label_metrics = {k: int(v or 0) for k, v in dict(labels).items()}

            expected = stage_d["results"]["exploratory_fixed_index_recurrent_stroke_31_365d"]
            for key in ("eligible", "source_events", "omop_events", "label_agreement"):
                if label_metrics[key] != int(expected[key]):
                    raise RuntimeError(f"Diagnostic failed to reproduce Stage D recurrent metric {key}: {label_metrics[key]} vs {expected[key]}")

            source_only = con.execute(text("""
            WITH d AS (
              SELECT a.* FROM #candidate_audit a
              JOIN #patient_labels p ON p.patid=a.patid
              WHERE p.source_positive=1 AND p.omop_positive=0
            ), patient_flags AS (
              SELECT patid,
                     MAX(has_visit_xwalk) any_visit_xwalk,
                     MAX(has_visit) any_visit,
                     MAX(omop_visit_acute) any_acute_visit,
                     MAX(omop_visit_in_window) any_visit_in_window,
                     MAX(has_condition_xwalk) any_condition_xwalk,
                     MAX(has_linked_condition) any_linked_condition,
                     MIN(source_day) min_source_day,
                     MAX(source_day) max_source_day
              FROM d GROUP BY patid
            )
            SELECT COUNT_BIG(*) patients,
                   SUM(CASE WHEN any_visit_xwalk=0 THEN 1 ELSE 0 END) no_visit_xwalk,
                   SUM(CASE WHEN any_visit=0 THEN 1 ELSE 0 END) no_omop_visit,
                   SUM(CASE WHEN any_acute_visit=0 THEN 1 ELSE 0 END) no_acute_omop_visit,
                   SUM(CASE WHEN any_visit_in_window=0 THEN 1 ELSE 0 END) no_omop_visit_in_31_365_window,
                   SUM(CASE WHEN any_condition_xwalk=0 THEN 1 ELSE 0 END) no_condition_xwalk,
                   SUM(CASE WHEN any_linked_condition=0 THEN 1 ELSE 0 END) no_linked_condition,
                   SUM(CASE WHEN min_source_day=31 OR max_source_day=31 THEN 1 ELSE 0 END) touches_day31,
                   SUM(CASE WHEN min_source_day=365 OR max_source_day=365 THEN 1 ELSE 0 END) touches_day365,
                   MIN(min_source_day) min_source_day,
                   MAX(max_source_day) max_source_day
            FROM patient_flags
            """)).mappings().one()
            source_only_metrics = {k: (int(v) if v is not None else None) for k, v in dict(source_only).items()}

            mapping = con.execute(text("""
            SELECT COUNT_BIG(*) source_candidate_rows,
                   SUM(CASE WHEN has_visit_xwalk=1 THEN 1 ELSE 0 END) rows_with_visit_xwalk,
                   SUM(CASE WHEN has_visit=1 THEN 1 ELSE 0 END) rows_with_omop_visit,
                   SUM(CASE WHEN omop_visit_acute=1 THEN 1 ELSE 0 END) rows_with_acute_omop_visit,
                   SUM(CASE WHEN omop_visit_in_window=1 THEN 1 ELSE 0 END) rows_with_omop_visit_in_window,
                   SUM(CASE WHEN has_condition_xwalk=1 THEN 1 ELSE 0 END) rows_with_condition_xwalk,
                   SUM(CASE WHEN has_linked_condition=1 THEN 1 ELSE 0 END) rows_with_linked_condition,
                   SUM(CASE WHEN source_day=31 THEN 1 ELSE 0 END) candidate_rows_day31,
                   SUM(CASE WHEN source_day=365 THEN 1 ELSE 0 END) candidate_rows_day365
            FROM #candidate_audit
            """)).mappings().one()
            mapping_metrics = {k: int(v or 0) for k, v in dict(mapping).items()}

            discharge = con.execute(text("""
            SELECT COUNT_BIG(*) eligible,
                   SUM(CASE WHEN discharge_date=omop_discharge_date THEN 1 ELSE 0 END) exact_discharge_date,
                   SUM(CASE WHEN discharge_date<>omop_discharge_date THEN 1 ELSE 0 END) discordant_discharge_date,
                   MAX(ABS(DATEDIFF(day,discharge_date,omop_discharge_date))) max_absolute_discharge_day_difference
            FROM #fixed365
            """)).mappings().one()
            discharge_metrics = {k: int(v or 0) for k, v in dict(discharge).items()}

    finally:
        engine.dispose()

    payload = {
        "status": "stage_d_stroke_recurrent_discordance_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "inherited_d0_definition_sha256": _sha256(D0_PATH),
        "parent_stage_d_analysis_git_sha": stage_d.get("analysis_git_sha"),
        "diagnostic_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "reproduced_recurrent_metrics": label_metrics,
        "source_only_mechanism_counts": source_only_metrics,
        "source_candidate_lineage": mapping_metrics,
        "index_discharge_date_agreement": discharge_metrics,
        "interpretation_guardrail": "This post-outcome diagnostic explains the five recurrent-stroke label discordances observed under the locked Stage D definition. It does not alter Stage D eligibility, outcome definitions, or equivalence margins.",
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "stage_d_stroke_recurrent_discordance.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_d_stroke_recurrent_discordance_complete")
    print(f"reproduced_recurrent_metrics: {label_metrics}")
    print(f"source_only_mechanism_counts: {source_only_metrics}")
    print(f"source_candidate_lineage: {mapping_metrics}")
    print(f"index_discharge_date_agreement: {discharge_metrics}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage D recurrent-stroke discordance diagnostic")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
