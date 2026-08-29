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
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_c_stroke_d1_d3_v1.json")
LIPID_ARTIFACT = Path("study_definitions/artifacts/stage_c_lipid_loinc_whitelist_v1.csv")
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
        text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
        {"s": schema, "t": table},
    ).fetchall()
    return {str(r[0]).upper() for r in rows}


def _load_loincs() -> list[str]:
    with LIPID_ARTIFACT.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return sorted({str(r.get("LOINC_NUM") or "").strip().upper() for r in reader if str(r.get("LOINC_NUM") or "").strip()})


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values: set[str] | frozenset[str] | list[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _pct(n: int, d: int) -> float | None:
    return None if d == 0 else 100.0 * n / d


def _cohort_metrics(con, source_table: str, omop_table: str) -> dict[str, Any]:
    row = con.execute(text(f"""
        WITH s AS (SELECT patid,index_date FROM {source_table}),
             o AS (SELECT patid,index_date FROM {omop_table}),
             u AS (SELECT patid FROM s UNION SELECT patid FROM o)
        SELECT
          (SELECT COUNT_BIG(*) FROM s) AS source_patients,
          (SELECT COUNT_BIG(*) FROM o) AS omop_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) AS intersection_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NULL THEN 1 ELSE 0 END) AS source_only_patients,
          SUM(CASE WHEN s.patid IS NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) AS omop_only_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND s.index_date=o.index_date THEN 1 ELSE 0 END) AS exact_date_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND ABS(DATEDIFF(day,s.index_date,o.index_date))<=1 THEN 1 ELSE 0 END) AS within1_date_patients
        FROM u LEFT JOIN s ON s.patid=u.patid LEFT JOIN o ON o.patid=u.patid
    """)).mappings().one()
    d = {k: int(v or 0) for k, v in dict(row).items()}
    union = d["intersection_patients"] + d["source_only_patients"] + d["omop_only_patients"]
    return {
        **d,
        "union_patients": union,
        "patient_jaccard": None if union == 0 else d["intersection_patients"] / union,
        "positive_agreement_percent": _pct(2 * d["intersection_patients"], d["source_patients"] + d["omop_patients"]),
        "exact_index_date_percent_among_shared": _pct(d["exact_date_patients"], d["intersection_patients"]),
        "within_1_day_percent_among_shared": _pct(d["within1_date_patients"], d["intersection_patients"]),
    }


def _portable_metrics(con, source_table: str, native_table: str) -> dict[str, Any]:
    row = con.execute(text(f"""
        WITH s AS (SELECT patid FROM {source_table}),
             n AS (SELECT patid FROM {native_table}),
             u AS (SELECT patid FROM s UNION SELECT patid FROM n)
        SELECT
          (SELECT COUNT_BIG(*) FROM s) AS source_patients,
          (SELECT COUNT_BIG(*) FROM n) AS native_omop_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND n.patid IS NOT NULL THEN 1 ELSE 0 END) AS intersection_patients,
          SUM(CASE WHEN s.patid IS NOT NULL AND n.patid IS NULL THEN 1 ELSE 0 END) AS source_only_patients,
          SUM(CASE WHEN s.patid IS NULL AND n.patid IS NOT NULL THEN 1 ELSE 0 END) AS native_only_patients
        FROM u LEFT JOIN s ON s.patid=u.patid LEFT JOIN n ON n.patid=u.patid
    """)).mappings().one()
    d = {k: int(v or 0) for k, v in dict(row).items()}
    union = d["intersection_patients"] + d["source_only_patients"] + d["native_only_patients"]
    return {
        **d,
        "union_patients": union,
        "patient_jaccard": None if union == 0 else d["intersection_patients"] / union,
        "positive_agreement_percent": _pct(2 * d["intersection_patients"], d["source_patients"] + d["native_omop_patients"]),
    }


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_DEFINITION.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-c-stroke-d1-d3-v1":
        raise RuntimeError("D1/D3 concordance requires stage-c-stroke-d1-d3-v1")
    if study.get("status") != "prespecified_before_stage_c_d1_d3_outcome_queries":
        raise RuntimeError("D1/D3 definition is not prespecified before outcome queries")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("D1/D3 frozen ETL SHA mismatch")

    preflight_path = cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d1_d3" / "stage_c_stroke_d1_d3_preflight.json"
    if not preflight_path.exists():
        raise RuntimeError("Run the D1/D3 preflight before concordance")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight.get("outcome_query_performed") is not False:
        raise RuntimeError("D1/D3 preflight is not outcome-free")
    if preflight.get("study_definition_sha256") != _sha256(STUDY_DEFINITION):
        raise RuntimeError("D1/D3 definition changed after the recorded preflight; rerun preflight before concordance")

    sql_cfg = cfg.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    out = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d1_d3"
    out.mkdir(parents=True, exist_ok=True)

    lipid_loincs = _load_loincs()
    lipid_list = _sql_list(lipid_loincs)
    all_stroke = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    icd9 = _sql_list(ICD9_STROKE_CODES)
    icd10 = _sql_list(ICD10_STROKE_CODES)
    ct_mri = _sql_list(CT_CODES | MRI_CODES)
    mri = _sql_list(MRI_CODES)
    cpt_types = _sql_list(CPT_TYPES)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            required = [
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
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Missing required table [{schema}].[{table}]")

            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            selected_lab_date = next((c for c in LAB_DATE_PRIORITY if c in lab_cols), None)
            if selected_lab_date is None:
                raise RuntimeError("No prespecified lipid source date field is available")
            if selected_lab_date != preflight["source_evidence_rules"]["selected_lipid_date_field"]:
                raise RuntimeError("Selected lipid date field differs from preflight")

            print("progress: materializing D0-eligible source encounter candidates", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#d0_candidates') IS NOT NULL DROP TABLE #d0_candidates")
            con.exec_driver_sql(f"""
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255),d.PATID) AS patid,
                         CONVERT(nvarchar(255),d.ENCOUNTERID) AS encounterid,
                         CONVERT(nvarchar(255),d.DIAGNOSISID) AS diagnosisid,
                         CAST(d.DX_DATE AS date) AS dx_date,
                         {_norm('d.DX')} AS dx_norm,
                         ROW_NUMBER() OVER (
                           PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                         ) AS rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({all_stroke}) AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,x.dx_norm,
                       CAST(e.ADMIT_DATE AS date) AS admit_date,CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                       COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) AS source_index_date,
                       CAST(dm.BIRTH_DATE AS date) AS source_birth_date
                INTO #d0_candidates
                FROM dx_rank x
                JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                  ON CONVERT(nvarchar(255),e.PATID)=x.patid AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
                JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm ON CONVERT(nvarchar(255),dm.PATID)=x.patid
                WHERE x.rn=1 AND {_short('e.ENC_TYPE')} IN ('EI','IP')
                  AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
                  AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1
                  AND COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) IS NOT NULL;
                CREATE INDEX IX_d0_candidates ON #d0_candidates(patid,encounterid);
            """)

            print("progress: materializing locked PCORnet D1/D3 cohorts", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#src_enc') IS NOT NULL DROP TABLE #src_enc")
            con.exec_driver_sql(f"""
                SELECT d.*,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] px
                         WHERE CONVERT(nvarchar(255),px.PATID)=d.patid
                           AND {_norm('px.PX')} IN ({ct_mri}) AND {_short('px.PX_TYPE')} IN ({cpt_types})
                           AND px.PX_DATE IS NOT NULL
                           AND CAST(px.PX_DATE AS date) BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date
                       ) THEN 1 ELSE 0 END AS source_d1_imaging,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] px
                         WHERE CONVERT(nvarchar(255),px.PATID)=d.patid
                           AND {_norm('px.PX')} IN ({mri}) AND {_short('px.PX_TYPE')} IN ({cpt_types})
                           AND px.PX_DATE IS NOT NULL
                           AND CAST(px.PX_DATE AS date) BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date
                       ) THEN 1 ELSE 0 END AS source_d3_imaging,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                         WHERE CONVERT(nvarchar(255),l.PATID)=d.patid
                           AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                           AND l.[{selected_lab_date}] IS NOT NULL
                           AND CAST(l.[{selected_lab_date}] AS date) BETWEEN d.admit_date AND d.discharge_date
                       ) THEN 1 ELSE 0 END AS source_lipid
                INTO #src_enc FROM #d0_candidates d;
                CREATE INDEX IX_src_enc ON #src_enc(patid,source_index_date,encounterid);
            """)

            for phenotype, imaging_col in (("d1", "source_d1_imaging"), ("d3", "source_d3_imaging")):
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#src_{phenotype}') IS NOT NULL DROP TABLE #src_{phenotype}")
                con.exec_driver_sql(f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY source_index_date,encounterid) rn
                      FROM #src_enc WHERE {imaging_col}=1 AND source_lipid=1
                    )
                    SELECT patid,encounterid,diagnosisid,dx_date,admit_date,discharge_date,source_index_date AS index_date
                    INTO #src_{phenotype}
                    FROM q
                    WHERE rn=1 AND FLOOR(DATEDIFF(day,source_birth_date,source_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_src_{phenotype} ON #src_{phenotype}(patid);
                """)

            print("progress: materializing lineage-faithful OMOP encounter evidence", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_base') IS NOT NULL DROP TABLE #omop_base")
            con.exec_driver_sql(f"""
                SELECT d.patid,d.encounterid,d.diagnosisid,d.dx_date,d.admit_date,d.discharge_date,
                       p.person_id,CAST(p.birth_datetime AS date) AS target_birth_date,
                       v.visit_occurrence_id,CAST(v.visit_start_date AS date) AS target_admit_date,
                       CAST(v.visit_end_date AS date) AS target_discharge_date,
                       COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) AS target_index_date
                INTO #omop_base
                FROM #d0_candidates d
                JOIN [{target_schema}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
                JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                  ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=p.person_id
                 AND co.visit_occurrence_id=v.visit_occurrence_id;
                CREATE INDEX IX_omop_base ON #omop_base(patid,encounterid);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_enc') IS NOT NULL DROP TABLE #omop_enc")
            con.exec_driver_sql(f"""
                SELECT b.*,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                      ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po
                      ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid
                      AND {_norm('sp.PX')} IN ({ct_mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_d1_imaging,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                      ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po
                      ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid
                      AND {_norm('sp.PX')} IN ({mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_d3_imaging,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_measurement_xwalk] mx
                      ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[measurement] m
                      ON m.measurement_id=mx.measurement_id AND m.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                      AND m.measurement_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) OR EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_observation_xwalk] ox
                      ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[observation] o
                      ON o.observation_id=ox.observation_id AND o.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                      AND o.observation_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_lipid
                INTO #omop_enc FROM #omop_base b;
                CREATE INDEX IX_omop_enc ON #omop_enc(patid,target_index_date,encounterid);
            """)

            for phenotype, imaging_col in (("d1", "target_d1_imaging"), ("d3", "target_d3_imaging")):
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#omop_{phenotype}') IS NOT NULL DROP TABLE #omop_{phenotype}")
                con.exec_driver_sql(f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY target_index_date,encounterid) rn
                      FROM #omop_enc
                      WHERE {imaging_col}=1 AND target_lipid=1 AND target_index_date IS NOT NULL AND target_birth_date IS NOT NULL
                    )
                    SELECT patid,encounterid,target_index_date AS index_date
                    INTO #omop_{phenotype}
                    FROM q
                    WHERE rn=1 AND FLOOR(DATEDIFF(day,target_birth_date,target_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_omop_{phenotype} ON #omop_{phenotype}(patid);
                """)

            print("progress: resolving native OMOP concept sets", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#stroke_std') IS NOT NULL DROP TABLE #stroke_std")
            con.exec_driver_sql(f"""
                SELECT DISTINCT concept_id INTO #stroke_std FROM (
                  SELECT c.concept_id FROM [{target_schema}].[concept] c
                  WHERE c.invalid_reason IS NULL AND c.standard_concept='S' AND c.domain_id='Condition'
                    AND ((c.vocabulary_id='ICD10CM' AND REPLACE(UPPER(c.concept_code),'.','') IN ({icd10}))
                      OR (c.vocabulary_id='ICD9CM' AND REPLACE(UPPER(c.concept_code),'.','') IN ({icd9})))
                  UNION ALL
                  SELECT t.concept_id FROM [{target_schema}].[concept] s
                  JOIN [{target_schema}].[concept_relationship] cr ON cr.concept_id_1=s.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
                  JOIN [{target_schema}].[concept] t ON t.concept_id=cr.concept_id_2 AND t.invalid_reason IS NULL AND t.standard_concept='S' AND t.domain_id='Condition'
                  WHERE s.invalid_reason IS NULL AND ((s.vocabulary_id='ICD10CM' AND REPLACE(UPPER(s.concept_code),'.','') IN ({icd10})) OR (s.vocabulary_id='ICD9CM' AND REPLACE(UPPER(s.concept_code),'.','') IN ({icd9})))
                ) q;
                CREATE UNIQUE CLUSTERED INDEX IX_stroke_std ON #stroke_std(concept_id);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#img_std') IS NOT NULL DROP TABLE #img_std")
            con.exec_driver_sql(f"""
                SELECT DISTINCT code_group,concept_id INTO #img_std FROM (
                  SELECT CASE WHEN REPLACE(UPPER(s.concept_code),'.','') IN ({mri}) THEN 'MRI' ELSE 'CT' END AS code_group,
                         CASE WHEN s.standard_concept='S' AND s.domain_id='Procedure' THEN s.concept_id ELSE t.concept_id END AS concept_id
                  FROM [{target_schema}].[concept] s
                  LEFT JOIN [{target_schema}].[concept_relationship] cr ON cr.concept_id_1=s.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
                  LEFT JOIN [{target_schema}].[concept] t ON t.concept_id=cr.concept_id_2 AND t.invalid_reason IS NULL AND t.standard_concept='S' AND t.domain_id='Procedure'
                  WHERE s.vocabulary_id IN ('CPT4','HCPCS') AND s.invalid_reason IS NULL
                    AND REPLACE(UPPER(s.concept_code),'.','') IN ({ct_mri})
                ) q WHERE concept_id IS NOT NULL;
                CREATE UNIQUE CLUSTERED INDEX IX_img_std ON #img_std(code_group,concept_id);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#lipid_std') IS NOT NULL DROP TABLE #lipid_std")
            con.exec_driver_sql(f"""
                SELECT DISTINCT target_domain,concept_id INTO #lipid_std FROM (
                  SELECT CASE WHEN s.standard_concept='S' AND s.domain_id IN ('Measurement','Observation') THEN s.domain_id ELSE t.domain_id END AS target_domain,
                         CASE WHEN s.standard_concept='S' AND s.domain_id IN ('Measurement','Observation') THEN s.concept_id ELSE t.concept_id END AS concept_id
                  FROM [{target_schema}].[concept] s
                  LEFT JOIN [{target_schema}].[concept_relationship] cr ON cr.concept_id_1=s.concept_id AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
                  LEFT JOIN [{target_schema}].[concept] t ON t.concept_id=cr.concept_id_2 AND t.invalid_reason IS NULL AND t.standard_concept='S' AND t.domain_id IN ('Measurement','Observation')
                  WHERE s.vocabulary_id='LOINC' AND s.invalid_reason IS NULL AND UPPER(LTRIM(RTRIM(s.concept_code))) IN ({lipid_list})
                ) q WHERE concept_id IS NOT NULL;
                CREATE UNIQUE CLUSTERED INDEX IX_lipid_std ON #lipid_std(target_domain,concept_id);
            """)

            print("progress: materializing native-OMOP D1/D3 portability sensitivities", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#native_enc') IS NOT NULL DROP TABLE #native_enc")
            con.exec_driver_sql(f"""
                SELECT DISTINCT p.person_id,CONVERT(nvarchar(255),p.person_source_value) AS patid,
                       v.visit_occurrence_id,CAST(v.visit_start_date AS date) AS index_date,
                       CAST(v.visit_start_date AS date) AS admit_date,CAST(v.visit_end_date AS date) AS discharge_date,
                       CAST(p.birth_datetime AS date) AS birth_date,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{target_schema}].[procedure_occurrence] po
                         JOIN #img_std i ON i.concept_id=po.procedure_concept_id
                         WHERE po.person_id=p.person_id AND po.procedure_date BETWEEN DATEADD(day,-2,CAST(v.visit_start_date AS date)) AND CAST(v.visit_end_date AS date)
                       ) THEN 1 ELSE 0 END AS d1_img,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{target_schema}].[procedure_occurrence] po
                         JOIN #img_std i ON i.concept_id=po.procedure_concept_id AND i.code_group='MRI'
                         WHERE po.person_id=p.person_id AND po.procedure_date BETWEEN DATEADD(day,-2,CAST(v.visit_start_date AS date)) AND CAST(v.visit_end_date AS date)
                       ) THEN 1 ELSE 0 END AS d3_img,
                       CASE WHEN EXISTS (
                         SELECT 1 FROM [{target_schema}].[measurement] m JOIN #lipid_std l ON l.target_domain='Measurement' AND l.concept_id=m.measurement_concept_id
                         WHERE m.person_id=p.person_id AND m.measurement_date BETWEEN CAST(v.visit_start_date AS date) AND CAST(v.visit_end_date AS date)
                       ) OR EXISTS (
                         SELECT 1 FROM [{target_schema}].[observation] o JOIN #lipid_std l ON l.target_domain='Observation' AND l.concept_id=o.observation_concept_id
                         WHERE o.person_id=p.person_id AND o.observation_date BETWEEN CAST(v.visit_start_date AS date) AND CAST(v.visit_end_date AS date)
                       ) THEN 1 ELSE 0 END AS lipid
                INTO #native_enc
                FROM [{target_schema}].[condition_occurrence] co
                JOIN #stroke_std ss ON ss.concept_id=co.condition_concept_id
                JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=co.visit_occurrence_id
                JOIN [{target_schema}].[person] p ON p.person_id=co.person_id
                WHERE v.visit_concept_id IN (262,9201)
                  AND v.visit_start_date IS NOT NULL AND v.visit_end_date IS NOT NULL
                  AND DATEDIFF(day,CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date))>=1
                  AND p.birth_datetime IS NOT NULL;
                CREATE INDEX IX_native_enc ON #native_enc(person_id,index_date,visit_occurrence_id);
            """)
            for phenotype, img_col in (("d1", "d1_img"), ("d3", "d3_img")):
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#native_{phenotype}') IS NOT NULL DROP TABLE #native_{phenotype}")
                con.exec_driver_sql(f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER(PARTITION BY person_id ORDER BY index_date,visit_occurrence_id) rn
                      FROM #native_enc WHERE {img_col}=1 AND lipid=1
                    )
                    SELECT patid,index_date INTO #native_{phenotype}
                    FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_native_{phenotype} ON #native_{phenotype}(patid);
                """)

            print("progress: computing D1/D3 cohort metrics and discordance", flush=True)
            results: dict[str, Any] = {}
            for phenotype in ("d1", "d3"):
                primary = _cohort_metrics(con, f"#src_{phenotype}", f"#omop_{phenotype}")
                portable = _portable_metrics(con, f"#src_{phenotype}", f"#native_{phenotype}")
                target_img_pred = "e.target_d1_imaging=1" if phenotype == "d1" else "e.target_d3_imaging=1"
                omop_img_vs_source_pred = (
                    "e.target_d1_imaging=1 AND s.source_d1_imaging=0"
                    if phenotype == "d1"
                    else "e.target_d3_imaging=1 AND s.source_d3_imaging=0"
                )

                source_only = [dict(r) for r in con.execute(text(f"""
                    WITH s AS (SELECT * FROM #src_{phenotype} WHERE patid NOT IN (SELECT patid FROM #omop_{phenotype}))
                    SELECT category,COUNT_BIG(*) AS patients FROM (
                      SELECT s.patid,
                        CASE
                          WHEN s.dx_date IS NULL THEN 'required_source_date_missing_or_etl_excluded'
                          WHEN NOT EXISTS (SELECT 1 FROM #omop_base b WHERE b.patid=s.patid AND b.encounterid=s.encounterid) THEN
                            CASE
                              WHEN NOT EXISTS (SELECT 1 FROM [{target_schema}].[etl_visit_occurrence_xwalk] vx WHERE CONVERT(nvarchar(255),vx.encounterid)=s.encounterid) THEN 'visit_not_materialized_or_unlinked'
                              ELSE 'stroke_diagnosis_not_materialized_or_unlinked'
                            END
                          WHEN NOT EXISTS (
                            SELECT 1 FROM #omop_enc e WHERE e.patid=s.patid AND e.encounterid=s.encounterid AND {target_img_pred}
                          ) THEN 'imaging_target_semantic_not_materialized_or_unlinked'
                          WHEN EXISTS (
                            SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                            WHERE CONVERT(nvarchar(255),l.PATID)=s.patid
                              AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                              AND l.[{selected_lab_date}] IS NOT NULL
                              AND CAST(l.[{selected_lab_date}] AS date) BETWEEN s.admit_date AND s.discharge_date
                              AND l.RESULT_DATE IS NULL
                          ) AND NOT EXISTS (
                            SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                            WHERE CONVERT(nvarchar(255),l.PATID)=s.patid
                              AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                              AND l.[{selected_lab_date}] IS NOT NULL
                              AND CAST(l.[{selected_lab_date}] AS date) BETWEEN s.admit_date AND s.discharge_date
                              AND l.RESULT_DATE IS NOT NULL
                          ) THEN 'lipid_source_event_missing_or_etl_excluded'
                          WHEN EXISTS (
                            SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                            LEFT JOIN [{target_schema}].[etl_measurement_xwalk] mx ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                            LEFT JOIN [{target_schema}].[measurement] m ON m.measurement_id=mx.measurement_id
                            LEFT JOIN [{target_schema}].[etl_observation_xwalk] ox ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                            LEFT JOIN [{target_schema}].[observation] o ON o.observation_id=ox.observation_id
                            WHERE CONVERT(nvarchar(255),l.PATID)=s.patid
                              AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                              AND l.[{selected_lab_date}] IS NOT NULL
                              AND CAST(l.[{selected_lab_date}] AS date) BETWEEN s.admit_date AND s.discharge_date
                              AND (m.measurement_id IS NOT NULL OR o.observation_id IS NOT NULL)
                          ) THEN 'lipid_date_representation_difference'
                          ELSE 'lipid_target_semantic_not_materialized_or_unlinked'
                        END AS category
                      FROM s
                    ) q GROUP BY category ORDER BY category
                """)).mappings().all()]
                for r in source_only:
                    r["patients"] = int(r["patients"] or 0)

                omop_only = [dict(r) for r in con.execute(text(f"""
                    WITH o AS (SELECT * FROM #omop_{phenotype} WHERE patid NOT IN (SELECT patid FROM #src_{phenotype}))
                    SELECT category,COUNT_BIG(*) AS patients FROM (
                      SELECT o.patid,
                        CASE
                          WHEN EXISTS (
                            SELECT 1 FROM #omop_enc e
                            JOIN #src_enc s ON s.patid=e.patid AND s.encounterid=e.encounterid
                            WHERE e.patid=o.patid AND e.target_lipid=1 AND s.source_lipid=0
                          ) THEN 'lipid_date_representation_difference'
                          WHEN EXISTS (
                            SELECT 1 FROM #omop_enc e
                            JOIN #src_enc s ON s.patid=e.patid AND s.encounterid=e.encounterid
                            WHERE e.patid=o.patid AND {omop_img_vs_source_pred}
                          ) THEN 'imaging_date_representation_difference'
                          ELSE 'multiple_qualifying_event_ordering_difference'
                        END AS category
                      FROM o
                    ) q GROUP BY category ORDER BY category
                """)).mappings().all()]
                for r in omop_only:
                    r["patients"] = int(r["patients"] or 0)

                results[phenotype.upper()] = {
                    "primary_transformation_fidelity": primary,
                    "primary_source_only_discordance_categories": source_only,
                    "primary_omop_only_discordance_categories": omop_only,
                    "secondary_native_omop_portability": portable,
                }

    finally:
        engine.dispose()

    summary: dict[str, Any] = {
        "status": "stage_c_stroke_d1_d3_concordance_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_DEFINITION),
        "lipid_artifact_sha256": _sha256(LIPID_ARTIFACT),
        "selected_lipid_date_field": selected_lab_date,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "D1": results["D1"],
        "D3": results["D3"],
        "interpretation_rules": [
            "Source D1/D3 use the locked exact source-code, PDX, encounter, overnight, imaging-window, lipid-window, ordering, and post-index age rules.",
            "Primary OMOP transformation fidelity preserves source-only code/PDX identity through frozen lineage but applies phenotype windows to the native frozen OMOP dates actually materialized by the ETL.",
            "Lineage-linked source events crossing a window because source and target date representations differ are reported explicitly as evidence-date representation differences.",
            "The native portability sensitivities use active Standard concepts only and intentionally omit PDX.",
            "The 22 locked lipid LOINCs without active Standard Measurement/Observation targets remain quantified native-portability coverage gaps and are not removed from the source-reference phenotype.",
            "No patient-level rows are written; output is aggregate JSON only.",
        ],
    }
    out_json = out / "stage_c_stroke_d1_d3_concordance.json"
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print("status: stage_c_stroke_d1_d3_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"selected_lipid_date_field: {selected_lab_date}")
    for phenotype in ("D1", "D3"):
        p = summary[phenotype]["primary_transformation_fidelity"]
        n = summary[phenotype]["secondary_native_omop_portability"]
        print(f"{phenotype}_source_patients: {p['source_patients']}")
        print(f"{phenotype}_lineage_faithful_omop_patients: {p['omop_patients']}")
        print(f"{phenotype}_intersection_patients: {p['intersection_patients']}")
        print(f"{phenotype}_source_only_patients: {p['source_only_patients']}")
        print(f"{phenotype}_omop_only_patients: {p['omop_only_patients']}")
        print(f"{phenotype}_patient_jaccard: {p['patient_jaccard']}")
        print(f"{phenotype}_exact_index_date_patients: {p['exact_date_patients']}")
        print(f"{phenotype}_native_omop_portable_patients: {n['native_omop_patients']}")
    print(f"output: {out_json}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage C locked stroke D1/D3 concordance")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
