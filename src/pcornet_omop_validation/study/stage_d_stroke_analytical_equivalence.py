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


def _risk(events: int, eligible: int) -> float | None:
    return None if eligible == 0 else events / eligible


def _comparison(source_events: int, source_eligible: int, omop_events: int, omop_eligible: int, abs_margin_pp: float, rr_lo: float, rr_hi: float) -> dict[str, object]:
    sr = _risk(source_events, source_eligible)
    orisk = _risk(omop_events, omop_eligible)
    diff = None if sr is None or orisk is None else 100.0 * (orisk - sr)
    ratio = None if sr in (None, 0) or orisk is None else orisk / sr
    abs_ok = None if diff is None else abs(diff) <= abs_margin_pp
    rr_ok = None if ratio is None else rr_lo <= ratio <= rr_hi
    return {
        "source_eligible": source_eligible,
        "source_events": source_events,
        "source_risk": sr,
        "omop_eligible": omop_eligible,
        "omop_events": omop_events,
        "omop_risk": orisk,
        "absolute_risk_difference_percentage_points": diff,
        "relative_risk_ratio_omop_over_source": ratio,
        "absolute_margin_met": abs_ok,
        "relative_margin_met": rr_ok,
        "both_margins_met": None if abs_ok is None or rr_ok is None else bool(abs_ok and rr_ok),
        "absolute_margin_percentage_points": abs_margin_pp,
        "relative_margin": [rr_lo, rr_hi],
    }


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage D definition is not anchored to frozen ETL")
    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_d_analytical_equivalence"
    preflight_path = out_dir / "stage_d_stroke_preflight.json"
    if not preflight_path.exists():
        raise RuntimeError("Stage D preflight JSON is required before outcome analysis")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight.get("status") != "stage_d_stroke_preflight_ready":
        raise RuntimeError("Stage D preflight is not ready")
    if preflight.get("study_definition_sha256") != _sha256(STUDY_PATH):
        raise RuntimeError("Stage D study definition changed after preflight; rerun preflight")
    if preflight.get("inherited_d0_definition_sha256") != _sha256(D0_PATH):
        raise RuntimeError("Inherited D0 definition changed after preflight; rerun preflight")
    if preflight.get("outcome_query_performed") is not False or preflight.get("cross_cdm_outcome_query_performed") is not False:
        raise RuntimeError("Preflight does not document an outcome-free state")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    code_list = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    acute_source = "'ED','EI','IP'"
    acute_target = "9203,262,9201"

    engine = make_engine(cfg)
    results: dict[str, object] = {}
    try:
        with engine.connect() as con:
            print("progress: materializing inherited locked source D0 index cohort", flush=True)
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
              SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
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
            SELECT patid,encounterid,diagnosisid,dx_date,admit_date,discharge_date,index_date,birth_date
            INTO #src_d0 FROM ranked
            WHERE patient_rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_src_d0 ON #src_d0(patid);
            """)

            print("progress: materializing inherited lineage-faithful OMOP D0 index cohort", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_d0_all') IS NOT NULL DROP TABLE #omop_d0_all")
            con.exec_driver_sql(f"""
            SELECT s.patid,p.person_id,s.encounterid,s.index_date source_index_date,
                   CAST(v.visit_start_date AS date) omop_admit_date,CAST(v.visit_end_date AS date) omop_discharge_date,
                   COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) omop_index_date,
                   v.visit_occurrence_id,co.condition_occurrence_id
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
            SELECT patid,person_id,encounterid,source_index_date,omop_admit_date,omop_discharge_date,omop_index_date,visit_occurrence_id
            INTO #omop_d0 FROM r WHERE rn=1;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_omop_d0 ON #omop_d0(patid);
            """)

            source_n = int(con.execute(text("SELECT COUNT_BIG(*) FROM #src_d0")).scalar_one())
            omop_n = int(con.execute(text("SELECT COUNT_BIG(*) FROM #omop_d0")).scalar_one())
            shared_exact_n = int(con.execute(text("""
                SELECT COUNT_BIG(*) FROM #src_d0 s JOIN #omop_d0 o ON o.patid=s.patid WHERE s.index_date=o.omop_index_date
            """)).scalar_one())
            anchor = preflight["stage_c_d0_anchor"]
            if source_n != int(anchor["source_patients"]) or omop_n != int(anchor["lineage_faithful_omop_patients"]) or shared_exact_n != int(anchor["intersection_patients"]):
                raise RuntimeError(f"Stage D failed to reproduce Stage C D0 anchors: source={source_n}, omop={omop_n}, shared_exact={shared_exact_n}, expected={anchor}")

            print("progress: materializing representation-specific observability and acute-care labels", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_labels') IS NOT NULL DROP TABLE #src_labels")
            con.exec_driver_sql(f"""
            SELECT s.*,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en WHERE CONVERT(nvarchar(255),en.PATID)=s.patid AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,30,s.discharge_date)) THEN 1 ELSE 0 END covered30,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en WHERE CONVERT(nvarchar(255),en.PATID)=s.patid AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,90,s.discharge_date)) THEN 1 ELSE 0 END covered90,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en WHERE CONVERT(nvarchar(255),en.PATID)=s.patid AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,365,s.discharge_date)) THEN 1 ELSE 0 END covered365,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND CAST(e.ADMIT_DATE AS date)>s.discharge_date AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,30,s.discharge_date) AND {_short('e.ENC_TYPE')} IN ({acute_source})) THEN 1 ELSE 0 END event30,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND CAST(e.ADMIT_DATE AS date)>s.discharge_date AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,s.discharge_date) AND {_short('e.ENC_TYPE')} IN ({acute_source})) THEN 1 ELSE 0 END event90,
              (SELECT MIN(CAST(e.ADMIT_DATE AS date)) FROM [{source_schema}].[PCORnet_ENCOUNTER] e WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND CAST(e.ADMIT_DATE AS date)>s.discharge_date AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,s.discharge_date) AND {_short('e.ENC_TYPE')} IN ({acute_source})) first_event90_date,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d ON CONVERT(nvarchar(255),d.PATID)=CONVERT(nvarchar(255),e.PATID) AND CONVERT(nvarchar(255),d.ENCOUNTERID)=CONVERT(nvarchar(255),e.ENCOUNTERID)
                WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND CAST(e.ADMIT_DATE AS date)>=DATEADD(day,31,s.discharge_date) AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,365,s.discharge_date)
                  AND {_short('e.ENC_TYPE')} IN ({acute_source}) AND {_norm('d.DX')} IN ({code_list})
              ) THEN 1 ELSE 0 END recurrent365
            INTO #src_labels FROM #src_d0 s;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_src_labels ON #src_labels(patid);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_labels') IS NOT NULL DROP TABLE #omop_labels")
            con.exec_driver_sql(f"""
            SELECT o.*,
              CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[observation_period] op WHERE op.person_id=o.person_id AND op.observation_period_start_date<=o.omop_discharge_date AND op.observation_period_end_date>=DATEADD(day,30,o.omop_discharge_date)) THEN 1 ELSE 0 END covered30,
              CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[observation_period] op WHERE op.person_id=o.person_id AND op.observation_period_start_date<=o.omop_discharge_date AND op.observation_period_end_date>=DATEADD(day,90,o.omop_discharge_date)) THEN 1 ELSE 0 END covered90,
              CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[observation_period] op WHERE op.person_id=o.person_id AND op.observation_period_start_date<=o.omop_discharge_date AND op.observation_period_end_date>=DATEADD(day,365,o.omop_discharge_date)) THEN 1 ELSE 0 END covered365,
              CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[visit_occurrence] v WHERE v.person_id=o.person_id AND CAST(v.visit_start_date AS date)>o.omop_discharge_date AND CAST(v.visit_start_date AS date)<=DATEADD(day,30,o.omop_discharge_date) AND v.visit_concept_id IN ({acute_target})) THEN 1 ELSE 0 END event30,
              CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[visit_occurrence] v WHERE v.person_id=o.person_id AND CAST(v.visit_start_date AS date)>o.omop_discharge_date AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,o.omop_discharge_date) AND v.visit_concept_id IN ({acute_target})) THEN 1 ELSE 0 END event90,
              (SELECT MIN(CAST(v.visit_start_date AS date)) FROM [{target_schema}].[visit_occurrence] v WHERE v.person_id=o.person_id AND CAST(v.visit_start_date AS date)>o.omop_discharge_date AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,o.omop_discharge_date) AND v.visit_concept_id IN ({acute_target})) first_event90_date,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{target_schema}].[visit_occurrence] v
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON vx.visit_occurrence_id=v.visit_occurrence_id
                JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d ON CONVERT(nvarchar(255),d.ENCOUNTERID)=CONVERT(nvarchar(255),vx.encounterid)
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=CONVERT(nvarchar(255),d.DIAGNOSISID)
                JOIN [{target_schema}].[condition_occurrence] co ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=o.person_id AND co.visit_occurrence_id=v.visit_occurrence_id
                WHERE v.person_id=o.person_id AND CAST(v.visit_start_date AS date)>=DATEADD(day,31,o.omop_discharge_date) AND CAST(v.visit_start_date AS date)<=DATEADD(day,365,o.omop_discharge_date)
                  AND v.visit_concept_id IN ({acute_target}) AND {_norm('d.DX')} IN ({code_list})
              ) THEN 1 ELSE 0 END recurrent365
            INTO #omop_labels FROM #omop_d0 o;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_omop_labels ON #omop_labels(patid);
            """)

            def aggregate_fixed(window: int) -> dict[str, int]:
                row = con.execute(text(f"""
                WITH x AS (
                  SELECT s.patid,s.event{window} se,o.event{window} oe
                  FROM #src_labels s JOIN #omop_labels o ON o.patid=s.patid
                  WHERE s.index_date=o.omop_index_date AND s.covered{window}=1 AND o.covered{window}=1
                )
                SELECT COUNT_BIG(*) eligible,
                       SUM(CASE WHEN se=1 THEN 1 ELSE 0 END) source_events,
                       SUM(CASE WHEN oe=1 THEN 1 ELSE 0 END) omop_events,
                       SUM(CASE WHEN se=1 AND oe=1 THEN 1 ELSE 0 END) both_positive,
                       SUM(CASE WHEN se=1 AND oe=0 THEN 1 ELSE 0 END) source_only_positive,
                       SUM(CASE WHEN se=0 AND oe=1 THEN 1 ELSE 0 END) omop_only_positive,
                       SUM(CASE WHEN se=0 AND oe=0 THEN 1 ELSE 0 END) both_negative
                FROM x
                """)).mappings().one()
                return {k:int(v or 0) for k,v in dict(row).items()}

            def aggregate_end(window: int, table: str) -> tuple[int,int]:
                row = con.execute(text(f"SELECT COUNT_BIG(*) eligible,SUM(CASE WHEN event{window}=1 THEN 1 ELSE 0 END) events FROM {table} WHERE covered{window}=1")).mappings().one()
                return int(row["eligible"] or 0), int(row["events"] or 0)

            margins = study["equivalence_margins"]
            fixed90 = aggregate_fixed(90)
            fixed30 = aggregate_fixed(30)
            fixed90_cmp = _comparison(fixed90["source_events"], fixed90["eligible"], fixed90["omop_events"], fixed90["eligible"], float(margins["primary_90d_absolute_risk_difference_percentage_points"]), float(margins["primary_90d_relative_risk_ratio_lower"]), float(margins["primary_90d_relative_risk_ratio_upper"]))
            fixed30_cmp = _comparison(fixed30["source_events"], fixed30["eligible"], fixed30["omop_events"], fixed30["eligible"], float(margins["secondary_30d_absolute_risk_difference_percentage_points"]), float(margins["secondary_30d_relative_risk_ratio_lower"]), float(margins["secondary_30d_relative_risk_ratio_upper"]))

            s90e,s90n = aggregate_end(90,"#src_labels")
            o90e,o90n = aggregate_end(90,"#omop_labels")
            end90_cmp = _comparison(s90n,s90e,o90n,o90e,float(margins["primary_90d_absolute_risk_difference_percentage_points"]),float(margins["primary_90d_relative_risk_ratio_lower"]),float(margins["primary_90d_relative_risk_ratio_upper"]))
            s30e,s30n = aggregate_end(30,"#src_labels")
            o30e,o30n = aggregate_end(30,"#omop_labels")
            end30_cmp = _comparison(s30n,s30e,o30n,o30e,float(margins["secondary_30d_absolute_risk_difference_percentage_points"]),float(margins["secondary_30d_relative_risk_ratio_lower"]),float(margins["secondary_30d_relative_risk_ratio_upper"]))

            date_row = con.execute(text("""
            WITH x AS (
              SELECT s.first_event90_date sd,o.first_event90_date od
              FROM #src_labels s JOIN #omop_labels o ON o.patid=s.patid
              WHERE s.index_date=o.omop_index_date AND s.covered90=1 AND o.covered90=1 AND s.event90=1 AND o.event90=1
            )
            SELECT COUNT_BIG(*) both_positive,
                   SUM(CASE WHEN sd=od THEN 1 ELSE 0 END) exact_date,
                   SUM(CASE WHEN ABS(DATEDIFF(day,sd,od))<=1 THEN 1 ELSE 0 END) within1_date,
                   AVG(CAST(DATEDIFF(day, CAST('19000101' AS date), sd) AS float)) AS avg_source_serial,
                   AVG(CAST(DATEDIFF(day, CAST('19000101' AS date), od) AS float)) AS avg_omop_serial
            FROM x
            """)).mappings().one()
            date_metrics = {"both_positive":int(date_row["both_positive"] or 0),"exact_first_event_date":int(date_row["exact_date"] or 0),"within1_first_event_date":int(date_row["within1_date"] or 0)}

            recurrent = con.execute(text("""
            WITH fixed AS (
              SELECT s.patid,s.recurrent365 sr,o.recurrent365 orc
              FROM #src_labels s JOIN #omop_labels o ON o.patid=s.patid
              WHERE s.index_date=o.omop_index_date AND s.covered365=1 AND o.covered365=1
            )
            SELECT COUNT_BIG(*) eligible,
                   SUM(CASE WHEN sr=1 THEN 1 ELSE 0 END) source_events,
                   SUM(CASE WHEN orc=1 THEN 1 ELSE 0 END) omop_events,
                   SUM(CASE WHEN sr=orc THEN 1 ELSE 0 END) label_agreement
            FROM fixed
            """)).mappings().one()
            recurrent_metrics = {k:int(v or 0) for k,v in dict(recurrent).items()}

            results = {
                "D0_reproduction": {"source_patients":source_n,"lineage_faithful_omop_patients":omop_n,"shared_exact_index_patients":shared_exact_n},
                "primary_fixed_index_90d": {"label_counts":fixed90,"risk_equivalence":fixed90_cmp,"first_event_date_agreement":date_metrics},
                "secondary_fixed_index_30d": {"label_counts":fixed30,"risk_equivalence":fixed30_cmp},
                "secondary_end_to_end_90d": end90_cmp,
                "secondary_end_to_end_30d": end30_cmp,
                "exploratory_fixed_index_recurrent_stroke_31_365d": recurrent_metrics,
            }
    finally:
        engine.dispose()

    payload = {
        "status":"stage_d_stroke_analytical_equivalence_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "study_definition":study["study_definition"],
        "study_definition_sha256":_sha256(STUDY_PATH),
        "inherited_d0_definition_sha256":_sha256(D0_PATH),
        "preflight_analysis_git_sha":preflight.get("analysis_git_sha"),
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "results":results,
        "interpretation_guardrail":"Primary fixed-index comparison isolates post-index event representation by holding patient and index date fixed. End-to-end comparisons intentionally include the previously observed D0 phenotype attrition and must be interpreted as combined phenotype-plus-outcome analytical reproducibility.",
        "disclosure_review":{"aggregate_only_outputs":True,"patient_identifiers_written":False,"row_level_phi_written":False,"status":"passed"},
    }
    out = out_dir / "stage_d_stroke_analytical_equivalence.json"
    out.write_text(json.dumps(payload,indent=2,sort_keys=True),encoding="utf-8")
    print("status: stage_d_stroke_analytical_equivalence_complete")
    print(f"analysis_git_sha: {payload['analysis_git_sha']}")
    for key in ("primary_fixed_index_90d","secondary_fixed_index_30d","secondary_end_to_end_90d","secondary_end_to_end_30d"):
        print(f"{key}: {results[key]}")
    print(f"exploratory_fixed_index_recurrent_stroke_31_365d: {results['exploratory_fixed_index_recurrent_stroke_31_365d']}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p=argparse.ArgumentParser(description="Stage D stroke analytical equivalence")
    p.add_argument("--config",required=True)
    p.add_argument("--output-dir")
    a=p.parse_args()
    run(a.config,a.output_dir)


if __name__=="__main__":
    main()
