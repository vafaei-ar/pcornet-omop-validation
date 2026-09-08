from __future__ import annotations

"""Reviewer-inspired post-freeze Stage C encounter-date fallback sensitivity.

This analysis is intentionally non-destructive.  It does not alter the frozen OMOP
instance.  Instead, it constructs a session-local alternative target overlay for the
locked D0/D1/D3 stroke phenotypes.  Qualifying source diagnoses with a recorded
DX_DATE must still be represented by their frozen lineage-faithful condition event.
Qualifying diagnoses whose DX_DATE is missing are represented only in the sensitivity
overlay, using the linked target visit admission date and explicit provenance
``encounter_admission_date_fallback``.

All procedure/lipid evidence is required to be materially present in the frozen target
through the existing lineage tables.  Thus the only altered target rule is diagnosis
date eligibility/date assignment, exactly as locked in
``stage_c_d_encounter_date_fallback_sensitivity_v1.json``.
"""

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
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_c_d_encounter_date_fallback_sensitivity_v1.json")
LIPID_ARTIFACT = Path("study_definitions/artifacts/stage_c_lipid_loinc_whitelist_v1.csv")
CT_CODES = frozenset({"70450", "70460", "70470"})
MRI_CODES = frozenset({"70551", "70552", "70553", "70557", "70558", "70559"})
CPT_TYPES = frozenset({"CH", "CPT", "CPT4", "HCPCS"})
LAB_DATE_PRIORITY = ("LAB_TKN_DTTM", "SPECIMEN_DATE", "LAB_DATE", "RESULT_DATE")
EXPECTED_SOURCE = {"D0": 9815, "D1": 8624, "D3": 7565}


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
        {"s": schema, "t": table},
    ).fetchall()
    return {str(r[0]).upper() for r in rows}


def _load_loincs() -> list[str]:
    with LIPID_ARTIFACT.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return sorted(
            {
                str(r.get("LOINC_NUM") or "").strip().upper()
                for r in reader
                if str(r.get("LOINC_NUM") or "").strip()
            }
        )


def _metrics(con, src: str, tgt: str) -> dict[str, Any]:
    row = con.execute(
        text(
            f"""
            WITH s AS (SELECT patid,index_date FROM {src}),
                 t AS (SELECT patid,index_date FROM {tgt}),
                 u AS (SELECT patid FROM s UNION SELECT patid FROM t)
            SELECT
              (SELECT COUNT_BIG(*) FROM s) source_patients,
              (SELECT COUNT_BIG(*) FROM t) fallback_target_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NOT NULL THEN 1 ELSE 0 END) intersection_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NULL THEN 1 ELSE 0 END) source_only_patients,
              SUM(CASE WHEN s.patid IS NULL AND t.patid IS NOT NULL THEN 1 ELSE 0 END) fallback_only_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NOT NULL AND s.index_date=t.index_date THEN 1 ELSE 0 END) exact_date_patients,
              SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NOT NULL AND ABS(DATEDIFF(day,s.index_date,t.index_date))<=1 THEN 1 ELSE 0 END) within1_date_patients
            FROM u
            LEFT JOIN s ON s.patid=u.patid
            LEFT JOIN t ON t.patid=u.patid
            """
        )
    ).mappings().one()
    d = {k: int(v or 0) for k, v in dict(row).items()}
    union = d["intersection_patients"] + d["source_only_patients"] + d["fallback_only_patients"]
    shared = d["intersection_patients"]
    d["union_patients"] = union
    d["patient_jaccard"] = None if union == 0 else shared / union
    d["exact_index_date_percent_among_shared"] = None if shared == 0 else 100.0 * d["exact_date_patients"] / shared
    d["within_1_day_percent_among_shared"] = None if shared == 0 else 100.0 * d["within1_date_patients"] / shared
    return d


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("status") != "reviewer_inspired_post_freeze_sensitivity_locked_before_fallback_execution":
        raise RuntimeError("Fallback sensitivity was not locked before execution")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Fallback sensitivity is not anchored to the frozen ETL")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    stroke_codes = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    lipid_list = _sql_list(_load_loincs())
    ct_mri = _sql_list(CT_CODES | MRI_CODES)
    mri = _sql_list(MRI_CODES)
    cpt_types = _sql_list(CPT_TYPES)

    out_dir = (
        Path(output_dir)
        if output_dir
        else cfg.audit_dir.parent
        / "publication_analysis"
        / "stage_c_phenotypes"
        / "encounter_date_fallback_sensitivity"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            selected_lab_date = next((c for c in LAB_DATE_PRIORITY if c in lab_cols), None)
            if selected_lab_date is None:
                raise RuntimeError("No prespecified lipid date field is available")

            print("progress: materializing unchanged source-faithful stroke candidates", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_candidates') IS NOT NULL DROP TABLE #fb_candidates")
            con.exec_driver_sql(
                f"""
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255),d.PATID) AS patid,
                         CONVERT(nvarchar(255),d.ENCOUNTERID) AS encounterid,
                         CONVERT(nvarchar(255),d.DIAGNOSISID) AS diagnosisid,
                         CAST(d.DX_DATE AS date) AS dx_date,
                         ROW_NUMBER() OVER (
                           PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                                    CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                         ) AS rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({stroke_codes})
                    AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
                       CAST(e.ADMIT_DATE AS date) AS admit_date,
                       CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                       COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) AS source_index_date,
                       CAST(dm.BIRTH_DATE AS date) AS source_birth_date
                INTO #fb_candidates
                FROM dx_rank x
                JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                  ON CONVERT(nvarchar(255),e.PATID)=x.patid
                 AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
                JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm
                  ON CONVERT(nvarchar(255),dm.PATID)=x.patid
                WHERE x.rn=1
                  AND {_short('e.ENC_TYPE')} IN ('EI','IP')
                  AND e.ADMIT_DATE IS NOT NULL
                  AND e.DISCHARGE_DATE IS NOT NULL
                  AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1
                  AND COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) IS NOT NULL;
                CREATE INDEX IX_fb_candidates_patient ON #fb_candidates(patid,source_index_date,encounterid);
                CREATE INDEX IX_fb_candidates_episode ON #fb_candidates(patid,encounterid,diagnosisid);
                """
            )

            print("progress: materializing source procedure and lipid evidence once", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_proc_hits') IS NOT NULL DROP TABLE #fb_proc_hits")
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.admit_date,c.discharge_date,
                       LTRIM(RTRIM(CONVERT(nvarchar(255),px.PROCEDURESID))) AS source_procedure_id,
                       CAST(px.PX_DATE AS date) AS source_px_date,
                       CASE WHEN {_norm('px.PX')} IN ({ct_mri}) THEN 1 ELSE 0 END AS d1_code,
                       CASE WHEN {_norm('px.PX')} IN ({mri}) THEN 1 ELSE 0 END AS d3_code
                INTO #fb_proc_hits
                FROM #fb_candidates c
                JOIN [{source_schema}].[PCORnet_PROCEDURES] px
                  ON CONVERT(nvarchar(255),px.PATID)=c.patid
                WHERE {_norm('px.PX')} IN ({ct_mri})
                  AND {_short('px.PX_TYPE')} IN ({cpt_types});
                CREATE INDEX IX_fb_proc_hits_source ON #fb_proc_hits(source_procedure_id);
                CREATE INDEX IX_fb_proc_hits_episode ON #fb_proc_hits(patid,encounterid,diagnosisid);
                """
            )
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_lab_hits') IS NOT NULL DROP TABLE #fb_lab_hits")
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.admit_date,c.discharge_date,
                       LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID))) AS source_record_id,
                       CAST(l.[{selected_lab_date}] AS date) AS source_lab_date
                INTO #fb_lab_hits
                FROM #fb_candidates c
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON CONVERT(nvarchar(255),l.PATID)=c.patid
                WHERE UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list});
                CREATE INDEX IX_fb_lab_hits_source ON #fb_lab_hits(source_record_id);
                CREATE INDEX IX_fb_lab_hits_episode ON #fb_lab_hits(patid,encounterid,diagnosisid);
                """
            )

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_source_proc_flags') IS NOT NULL DROP TABLE #fb_source_proc_flags")
            con.exec_driver_sql(
                """
                SELECT patid,encounterid,diagnosisid,
                       MAX(CASE WHEN source_px_date IS NOT NULL
                                     AND source_px_date BETWEEN DATEADD(day,-2,admit_date) AND discharge_date
                                THEN d1_code ELSE 0 END) AS d1_img,
                       MAX(CASE WHEN source_px_date IS NOT NULL
                                     AND source_px_date BETWEEN DATEADD(day,-2,admit_date) AND discharge_date
                                THEN d3_code ELSE 0 END) AS d3_img
                INTO #fb_source_proc_flags
                FROM #fb_proc_hits
                GROUP BY patid,encounterid,diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_fb_source_proc_flags ON #fb_source_proc_flags(patid,encounterid,diagnosisid);
                """
            )
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_source_lipid_flags') IS NOT NULL DROP TABLE #fb_source_lipid_flags")
            con.exec_driver_sql(
                """
                SELECT patid,encounterid,diagnosisid,
                       MAX(CASE WHEN source_lab_date IS NOT NULL
                                     AND source_lab_date BETWEEN admit_date AND discharge_date
                                THEN 1 ELSE 0 END) AS lipid
                INTO #fb_source_lipid_flags
                FROM #fb_lab_hits
                GROUP BY patid,encounterid,diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_fb_source_lipid_flags ON #fb_source_lipid_flags(patid,encounterid,diagnosisid);
                """
            )
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_source_enc') IS NOT NULL DROP TABLE #fb_source_enc")
            con.exec_driver_sql(
                """
                SELECT c.*,
                       COALESCE(p.d1_img,0) AS d1_img,
                       COALESCE(p.d3_img,0) AS d3_img,
                       COALESCE(l.lipid,0) AS lipid
                INTO #fb_source_enc
                FROM #fb_candidates c
                LEFT JOIN #fb_source_proc_flags p
                  ON p.patid=c.patid AND p.encounterid=c.encounterid AND p.diagnosisid=c.diagnosisid
                LEFT JOIN #fb_source_lipid_flags l
                  ON l.patid=c.patid AND l.encounterid=c.encounterid AND l.diagnosisid=c.diagnosisid;
                CREATE INDEX IX_fb_source_enc ON #fb_source_enc(patid,source_index_date,encounterid);
                """
            )

            def make_source(name: str, evidence: str) -> None:
                table = f"#fb_src_{name}"
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}")
                con.exec_driver_sql(
                    f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY source_index_date,encounterid) AS rn
                      FROM #fb_source_enc
                      WHERE {evidence}
                    )
                    SELECT patid,encounterid,diagnosisid,dx_date,admit_date,discharge_date,
                           source_index_date AS index_date
                    INTO {table}
                    FROM q
                    WHERE rn=1
                      AND FLOOR(DATEDIFF(day,source_birth_date,source_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_fb_src_{name} ON {table}(patid);
                    """
                )

            make_source("d0", "1=1")
            make_source("d1", "d1_img=1 AND lipid=1")
            make_source("d3", "d3_img=1 AND lipid=1")

            print("progress: constructing non-destructive fallback target diagnosis overlay", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_existing_condition') IS NOT NULL DROP TABLE #fb_existing_condition")
            con.exec_driver_sql(
                f"""
                ;WITH q AS (
                  SELECT c.patid,c.encounterid,c.diagnosisid,
                         CAST(co.condition_start_date AS date) AS condition_start_date,
                         co.condition_occurrence_id,
                         ROW_NUMBER() OVER (
                           PARTITION BY c.patid,c.encounterid,c.diagnosisid
                           ORDER BY co.condition_occurrence_id
                         ) AS rn
                  FROM #fb_candidates c
                  JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                    ON cx.source_domain='DIAGNOSIS'
                   AND CONVERT(nvarchar(255),cx.source_record_id)=c.diagnosisid
                  JOIN [{target_schema}].[condition_occurrence] co
                    ON co.condition_occurrence_id=cx.condition_occurrence_id
                )
                SELECT patid,encounterid,diagnosisid,condition_start_date,condition_occurrence_id
                INTO #fb_existing_condition
                FROM q WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_fb_existing_condition ON #fb_existing_condition(patid,encounterid,diagnosisid);
                """
            )

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_target_base') IS NOT NULL DROP TABLE #fb_target_base")
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.dx_date,c.admit_date,c.discharge_date,c.source_birth_date,
                       p.person_id,CAST(p.birth_datetime AS date) AS target_birth_date,
                       v.visit_occurrence_id,CAST(v.visit_start_date AS date) AS target_admit_date,
                       CAST(v.visit_end_date AS date) AS target_discharge_date,
                       CASE WHEN c.dx_date IS NOT NULL THEN ec.condition_start_date
                            ELSE CAST(v.visit_start_date AS date) END AS fallback_index_date,
                       CASE WHEN c.dx_date IS NOT NULL THEN 'recorded_diagnosis_date'
                            ELSE 'encounter_admission_date_fallback' END AS date_basis,
                       ec.condition_occurrence_id
                INTO #fb_target_base
                FROM #fb_candidates c
                JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(255),p.person_source_value)=c.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                  ON CONVERT(nvarchar(255),vx.encounterid)=c.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id=vx.visit_occurrence_id
                 AND v.person_id=p.person_id
                LEFT JOIN #fb_existing_condition ec
                  ON ec.patid=c.patid AND ec.encounterid=c.encounterid AND ec.diagnosisid=c.diagnosisid
                WHERE (c.dx_date IS NULL OR ec.condition_occurrence_id IS NOT NULL)
                  AND (c.dx_date IS NULL OR ec.condition_start_date IS NOT NULL);
                CREATE INDEX IX_fb_target_base_episode ON #fb_target_base(patid,encounterid,diagnosisid);
                CREATE INDEX IX_fb_target_base_person ON #fb_target_base(person_id,fallback_index_date);
                """
            )

            print("progress: mapping target procedure evidence through frozen lineage", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_target_proc_flags') IS NOT NULL DROP TABLE #fb_target_proc_flags")
            con.exec_driver_sql(
                f"""
                SELECT b.patid,b.encounterid,b.diagnosisid,
                       MAX(h.d1_code) AS d1_img,
                       MAX(h.d3_code) AS d3_img
                INTO #fb_target_proc_flags
                FROM #fb_target_base b
                JOIN #fb_proc_hits h
                  ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                  ON x.source_procedure_id=h.source_procedure_id
                JOIN [{target_schema}].[procedure_occurrence] po
                  ON po.procedure_occurrence_id=x.procedure_occurrence_id
                 AND po.person_id=b.person_id
                WHERE po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                GROUP BY b.patid,b.encounterid,b.diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_fb_target_proc_flags ON #fb_target_proc_flags(patid,encounterid,diagnosisid);
                """
            )

            print("progress: mapping target lipid evidence through frozen lineage", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_target_lipid_flags') IS NOT NULL DROP TABLE #fb_target_lipid_flags")
            con.exec_driver_sql(
                f"""
                ;WITH mapped AS (
                  SELECT b.patid,b.encounterid,b.diagnosisid
                  FROM #fb_target_base b
                  JOIN #fb_lab_hits h
                    ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                  JOIN [{target_schema}].[etl_measurement_xwalk] mx
                    ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=h.source_record_id
                  JOIN [{target_schema}].[measurement] m
                    ON m.measurement_id=mx.measurement_id AND m.person_id=b.person_id
                  WHERE m.measurement_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  UNION ALL
                  SELECT b.patid,b.encounterid,b.diagnosisid
                  FROM #fb_target_base b
                  JOIN #fb_lab_hits h
                    ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                  JOIN [{target_schema}].[etl_observation_xwalk] ox
                    ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=h.source_record_id
                  JOIN [{target_schema}].[observation] o
                    ON o.observation_id=ox.observation_id AND o.person_id=b.person_id
                  WHERE o.observation_date BETWEEN b.target_admit_date AND b.target_discharge_date
                )
                SELECT patid,encounterid,diagnosisid,1 AS lipid
                INTO #fb_target_lipid_flags
                FROM mapped
                GROUP BY patid,encounterid,diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_fb_target_lipid_flags ON #fb_target_lipid_flags(patid,encounterid,diagnosisid);
                """
            )

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#fb_target_enc') IS NOT NULL DROP TABLE #fb_target_enc")
            con.exec_driver_sql(
                """
                SELECT b.*,
                       COALESCE(p.d1_img,0) AS d1_img,
                       COALESCE(p.d3_img,0) AS d3_img,
                       COALESCE(l.lipid,0) AS lipid
                INTO #fb_target_enc
                FROM #fb_target_base b
                LEFT JOIN #fb_target_proc_flags p
                  ON p.patid=b.patid AND p.encounterid=b.encounterid AND p.diagnosisid=b.diagnosisid
                LEFT JOIN #fb_target_lipid_flags l
                  ON l.patid=b.patid AND l.encounterid=b.encounterid AND l.diagnosisid=b.diagnosisid;
                CREATE INDEX IX_fb_target_enc ON #fb_target_enc(patid,fallback_index_date,encounterid);
                """
            )

            def make_target(name: str, evidence: str) -> None:
                table = f"#fb_tgt_{name}"
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}")
                con.exec_driver_sql(
                    f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER(
                        PARTITION BY patid
                        ORDER BY fallback_index_date,encounterid,diagnosisid
                      ) AS rn
                      FROM #fb_target_enc
                      WHERE {evidence} AND fallback_index_date IS NOT NULL
                    )
                    SELECT patid,encounterid,diagnosisid,dx_date,target_admit_date,target_discharge_date,
                           fallback_index_date AS index_date,date_basis
                    INTO {table}
                    FROM q
                    WHERE rn=1
                      AND FLOOR(DATEDIFF(day,target_birth_date,fallback_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_fb_tgt_{name} ON {table}(patid);
                    """
                )

            make_target("d0", "1=1")
            make_target("d1", "d1_img=1 AND lipid=1")
            make_target("d3", "d3_img=1 AND lipid=1")

            print("progress: computing fallback Stage C concordance", flush=True)
            results: dict[str, Any] = {}
            provenance: dict[str, Any] = {}
            for phenotype in ("D0", "D1", "D3"):
                key = phenotype.lower()
                metrics = _metrics(con, f"#fb_src_{key}", f"#fb_tgt_{key}")
                if metrics["source_patients"] != EXPECTED_SOURCE[phenotype]:
                    raise RuntimeError(
                        f"{phenotype} source anchor mismatch: observed={metrics['source_patients']} expected={EXPECTED_SOURCE[phenotype]}"
                    )
                results[phenotype] = metrics
                rows = con.execute(
                    text(
                        f"""
                        SELECT date_basis,COUNT_BIG(*) AS n
                        FROM #fb_tgt_{key}
                        GROUP BY date_basis
                        ORDER BY date_basis
                        """
                    )
                ).mappings().all()
                provenance[phenotype] = {str(r["date_basis"]): int(r["n"]) for r in rows}

            overlay_rows = con.execute(
                text(
                    """
                    SELECT date_basis,COUNT_BIG(*) AS n
                    FROM #fb_target_base
                    GROUP BY date_basis
                    ORDER BY date_basis
                    """
                )
            ).mappings().all()
            overlay_provenance = {str(r["date_basis"]): int(r["n"]) for r in overlay_rows}
    finally:
        engine.dispose()

    payload: dict[str, object] = {
        "status": "stage_c_encounter_date_fallback_sensitivity_complete",
        "analysis_role": "reviewer_inspired_post_freeze_alternative_target_date_policy_sensitivity",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "study_definition": str(STUDY_PATH),
        "study_definition_sha256": _sha256(STUDY_PATH),
        "selected_source_lipid_date_field": selected_lab_date,
        "implementation": "session_local_fallback_overlay_set_based_v1",
        "results": results,
        "selected_target_episode_date_provenance": provenance,
        "all_candidate_target_overlay_date_provenance": overlay_provenance,
        "interpretation_guardrail": (
            "This reviewer-inspired post-freeze sensitivity changes only target diagnosis-date eligibility/date assignment. "
            "It does not modify the frozen target database, replace the primary Stage C analysis, or establish the fallback policy as uniquely correct or standard."
        ),
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "frozen_target_modified": False,
            "status": "passed",
        },
    }

    output_path = out_dir / "stage_c_encounter_date_fallback_sensitivity.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("status:", payload["status"])
    print(json.dumps(results, indent=2, sort_keys=True))
    print("selected target episode date provenance:")
    print(json.dumps(provenance, indent=2, sort_keys=True))
    print("output:", output_path)
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage C encounter-date fallback sensitivity")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
