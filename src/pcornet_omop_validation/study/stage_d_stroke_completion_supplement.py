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
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_src_d0 ON #src_d0(patid);
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
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_omop_d0 ON #omop_d0(patid);
            """)

            print("progress: materializing fixed-index 90-day eligible cohort", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fixed90') IS NOT NULL DROP TABLE #fixed90")
            con.exec_driver_sql(f"""
            SELECT s.patid,s.discharge_date,o.person_id,o.omop_discharge_date
            INTO #fixed90
            FROM #src_d0 s
            JOIN #omop_d0 o ON o.patid=s.patid AND o.omop_index_date=s.index_date
            WHERE EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                WHERE CONVERT(nvarchar(255),en.PATID)=s.patid
                  AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                  AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,90,s.discharge_date)
            )
              AND EXISTS (
                SELECT 1 FROM [{target_schema}].[observation_period] op
                WHERE op.person_id=o.person_id
                  AND op.observation_period_start_date<=o.omop_discharge_date
                  AND op.observation_period_end_date>=DATEADD(day,90,o.omop_discharge_date)
            );
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_fixed90 ON #fixed90(patid);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_first90') IS NOT NULL DROP TABLE #src_first90")
            con.exec_driver_sql(f"""
            SELECT f.patid,MIN(CAST(e.ADMIT_DATE AS date)) event_date,
                   DATEDIFF(day,f.discharge_date,MIN(CAST(e.ADMIT_DATE AS date))) event_day
            INTO #src_first90
            FROM #fixed90 f
            JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
              ON CONVERT(nvarchar(255),e.PATID)=f.patid
             AND CAST(e.ADMIT_DATE AS date)>f.discharge_date
             AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,f.discharge_date)
             AND {_short('e.ENC_TYPE')} IN ({acute_source})
            GROUP BY f.patid,f.discharge_date;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_src_first90 ON #src_first90(patid);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_first90') IS NOT NULL DROP TABLE #omop_first90")
            con.exec_driver_sql(f"""
            SELECT f.patid,MIN(CAST(v.visit_start_date AS date)) event_date,
                   DATEDIFF(day,f.omop_discharge_date,MIN(CAST(v.visit_start_date AS date))) event_day
            INTO #omop_first90
            FROM #fixed90 f
            JOIN [{target_schema}].[visit_occurrence] v
              ON v.person_id=f.person_id
             AND CAST(v.visit_start_date AS date)>f.omop_discharge_date
             AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,f.omop_discharge_date)
             AND v.visit_concept_id IN ({acute_target})
            GROUP BY f.patid,f.omop_discharge_date;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_omop_first90 ON #omop_first90(patid);
            """)

            fixed90 = con.execute(text("""
            SELECT (SELECT COUNT_BIG(*) FROM #fixed90) eligible,
                   (SELECT COUNT_BIG(*) FROM #src_first90) source_events,
                   (SELECT COUNT_BIG(*) FROM #omop_first90) omop_events,
                   (SELECT COUNT_BIG(*) FROM #src_first90 s JOIN #omop_first90 o ON o.patid=s.patid WHERE s.event_date=o.event_date) exact_first_event_date
            """)).mappings().one()
            fixed90_metrics = {k: int(v or 0) for k, v in dict(fixed90).items()}
            expected90 = stage_d["results"]["primary_fixed_index_90d"]
            if fixed90_metrics["eligible"] != int(expected90["label_counts"]["eligible"]):
                raise RuntimeError("Supplement failed to reproduce fixed-index 90-day eligible count")
            if fixed90_metrics["source_events"] != int(expected90["label_counts"]["source_events"]) or fixed90_metrics["omop_events"] != int(expected90["label_counts"]["omop_events"]):
                raise RuntimeError("Supplement failed to reproduce fixed-index 90-day event counts")
            if fixed90_metrics["exact_first_event_date"] != int(expected90["first_event_date_agreement"]["exact_first_event_date"]):
                raise RuntimeError("Supplement failed to reproduce exact first-event-date agreement")

            source_median = con.execute(text("""
            SELECT TOP 1 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY event_day) OVER () FROM #src_first90
            """)).scalar_one_or_none()
            omop_median = con.execute(text("""
            SELECT TOP 1 PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY event_day) OVER () FROM #omop_first90
            """)).scalar_one_or_none()
            time_to_event = {
                "eligible": fixed90_metrics["eligible"],
                "source_event_positive": fixed90_metrics["source_events"],
                "omop_event_positive": fixed90_metrics["omop_events"],
                "source_median_days_to_first_event": None if source_median is None else float(source_median),
                "omop_median_days_to_first_event": None if omop_median is None else float(omop_median),
                "exact_first_event_date": fixed90_metrics["exact_first_event_date"],
            }

            print("progress: materializing fixed-index 365-day cohort for recurrent PDX sensitivity", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fixed365') IS NOT NULL DROP TABLE #fixed365")
            con.exec_driver_sql(f"""
            SELECT s.patid,s.discharge_date,o.person_id,o.omop_discharge_date
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
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_fixed365 ON #fixed365(patid);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#recur_pdx') IS NOT NULL DROP TABLE #recur_pdx")
            con.exec_driver_sql(f"""
            SELECT f.patid,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d
                  ON CONVERT(nvarchar(255),d.PATID)=CONVERT(nvarchar(255),e.PATID)
                 AND CONVERT(nvarchar(255),d.ENCOUNTERID)=CONVERT(nvarchar(255),e.ENCOUNTERID)
                WHERE CONVERT(nvarchar(255),e.PATID)=f.patid
                  AND CAST(e.ADMIT_DATE AS date)>=DATEADD(day,31,f.discharge_date)
                  AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,365,f.discharge_date)
                  AND {_short('e.ENC_TYPE')} IN ({acute_source})
                  AND {_norm('d.DX')} IN ({code_list})
                  AND {_short('d.PDX')}='P'
              ) THEN 1 ELSE 0 END source_positive_pdx,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{target_schema}].[visit_occurrence] v
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON vx.visit_occurrence_id=v.visit_occurrence_id
                JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d ON CONVERT(nvarchar(255),d.ENCOUNTERID)=CONVERT(nvarchar(255),vx.encounterid)
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=CONVERT(nvarchar(255),d.DIAGNOSISID)
                JOIN [{target_schema}].[condition_occurrence] co ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=f.person_id AND co.visit_occurrence_id=v.visit_occurrence_id
                WHERE v.person_id=f.person_id
                  AND CAST(v.visit_start_date AS date)>=DATEADD(day,31,f.omop_discharge_date)
                  AND CAST(v.visit_start_date AS date)<=DATEADD(day,365,f.omop_discharge_date)
                  AND v.visit_concept_id IN ({acute_target})
                  AND {_norm('d.DX')} IN ({code_list})
                  AND {_short('d.PDX')}='P'
              ) THEN 1 ELSE 0 END omop_positive_pdx
            INTO #recur_pdx FROM #fixed365 f;
            CREATE UNIQUE CLUSTERED INDEX IX_stage_d_supp_recur_pdx ON #recur_pdx(patid);
            """)

            recurrent_pdx = con.execute(text("""
            SELECT COUNT_BIG(*) eligible,
                   SUM(CASE WHEN source_positive_pdx=1 THEN 1 ELSE 0 END) source_events,
                   SUM(CASE WHEN omop_positive_pdx=1 THEN 1 ELSE 0 END) omop_events,
                   SUM(CASE WHEN source_positive_pdx=omop_positive_pdx THEN 1 ELSE 0 END) label_agreement,
                   SUM(CASE WHEN source_positive_pdx=1 AND omop_positive_pdx=0 THEN 1 ELSE 0 END) source_only_positive,
                   SUM(CASE WHEN source_positive_pdx=0 AND omop_positive_pdx=1 THEN 1 ELSE 0 END) omop_only_positive
            FROM #recur_pdx
            """)).mappings().one()
            recurrent_pdx_metrics = {k: int(v or 0) for k, v in dict(recurrent_pdx).items()}

    finally:
        engine.dispose()

    payload = {
        "status": "stage_d_stroke_completion_supplement_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "inherited_d0_definition_sha256": _sha256(D0_PATH),
        "parent_stage_d_analysis_git_sha": stage_d.get("analysis_git_sha"),
        "supplement_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "prespecified_time_to_event_completion": time_to_event,
        "post_outcome_recurrent_pdx_sensitivity": recurrent_pdx_metrics,
        "protocol_note": "The locked Stage D definition listed median days to first 90-day event but the original completed JSON did not report it. This supplement reports that prespecified metric without changing the cohort or event definition. The recurrent PDX=P analysis is post-outcome sensitivity analysis only because the implemented exploratory recurrent endpoint used the locked stroke code set without a PDX filter.",
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "stage_d_stroke_completion_supplement.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_d_stroke_completion_supplement_complete")
    print(f"prespecified_time_to_event_completion: {time_to_event}")
    print(f"post_outcome_recurrent_pdx_sensitivity: {recurrent_pdx_metrics}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage D protocol-completion supplement")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
