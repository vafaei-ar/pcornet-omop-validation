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
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_c_stroke_harmonized_dxdate_sensitivity_v1.json")
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
    rows = con.execute(text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"), {"s": schema, "t": table}).fetchall()
    return {str(r[0]).upper() for r in rows}


def _load_loincs() -> list[str]:
    with LIPID_ARTIFACT.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return sorted({str(r.get("LOINC_NUM") or "").strip().upper() for r in reader if str(r.get("LOINC_NUM") or "").strip()})


def _metrics(con, src: str, omop: str) -> dict[str, Any]:
    row = con.execute(text(f"""
    WITH s AS (SELECT patid,index_date FROM {src}),
         o AS (SELECT patid,index_date FROM {omop}),
         u AS (SELECT patid FROM s UNION SELECT patid FROM o)
    SELECT
      (SELECT COUNT_BIG(*) FROM s) source_patients,
      (SELECT COUNT_BIG(*) FROM o) omop_patients,
      SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) intersection_patients,
      SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NULL THEN 1 ELSE 0 END) source_only_patients,
      SUM(CASE WHEN s.patid IS NULL AND o.patid IS NOT NULL THEN 1 ELSE 0 END) omop_only_patients,
      SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND s.index_date=o.index_date THEN 1 ELSE 0 END) exact_date_patients,
      SUM(CASE WHEN s.patid IS NOT NULL AND o.patid IS NOT NULL AND ABS(DATEDIFF(day,s.index_date,o.index_date))<=1 THEN 1 ELSE 0 END) within1_date_patients
    FROM u LEFT JOIN s ON s.patid=u.patid LEFT JOIN o ON o.patid=u.patid
    """)).mappings().one()
    d = {k: int(v or 0) for k, v in dict(row).items()}
    union = d["intersection_patients"] + d["source_only_patients"] + d["omop_only_patients"]
    shared = d["intersection_patients"]
    d["union_patients"] = union
    d["patient_jaccard"] = None if union == 0 else shared / union
    d["exact_index_date_percent_among_shared"] = None if shared == 0 else 100.0 * d["exact_date_patients"] / shared
    d["within_1_day_percent_among_shared"] = None if shared == 0 else 100.0 * d["within1_date_patients"] / shared
    return d


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("status") != "post_freeze_sensitivity_defined_before_execution":
        raise RuntimeError("Harmonized sensitivity definition is not locked before execution")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Frozen ETL SHA mismatch")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    all_stroke = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    lipid_list = _sql_list(_load_loincs())
    ct_mri = _sql_list(CT_CODES | MRI_CODES)
    mri = _sql_list(MRI_CODES)
    cpt_types = _sql_list(CPT_TYPES)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            selected_lab_date = next((c for c in LAB_DATE_PRIORITY if c in lab_cols), None)
            if selected_lab_date is None:
                raise RuntimeError("No prespecified lipid date field is available")

            print("progress: materializing harmonized non-null-DX_DATE stroke candidates", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#h_dx') IS NOT NULL DROP TABLE #h_dx")
            con.exec_driver_sql(f"""
            ;WITH dx_rank AS (
              SELECT CONVERT(nvarchar(255),d.PATID) patid,
                     CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
                     CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,
                     CAST(d.DX_DATE AS date) dx_date,
                     ROW_NUMBER() OVER (
                       PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                       ORDER BY CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                     ) rn
              FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
              WHERE d.DX_DATE IS NOT NULL AND {_norm('d.DX')} IN ({all_stroke}) AND {_short('d.PDX')}='P'
            )
            SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
                   CAST(e.ADMIT_DATE AS date) admit_date,CAST(e.DISCHARGE_DATE AS date) discharge_date,
                   CAST(dm.BIRTH_DATE AS date) birth_date
            INTO #h_dx
            FROM dx_rank x
            JOIN [{source_schema}].[PCORnet_ENCOUNTER] e ON CONVERT(nvarchar(255),e.PATID)=x.patid AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
            JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm ON CONVERT(nvarchar(255),dm.PATID)=x.patid
            WHERE x.rn=1 AND {_short('e.ENC_TYPE')} IN ('EI','IP')
              AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
              AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1;
            CREATE INDEX IX_h_dx ON #h_dx(patid,dx_date,encounterid);
            """)

            print("progress: materializing source evidence", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#h_src_enc') IS NOT NULL DROP TABLE #h_src_enc")
            con.exec_driver_sql(f"""
            SELECT d.*,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] px
                WHERE CONVERT(nvarchar(255),px.PATID)=d.patid AND {_norm('px.PX')} IN ({ct_mri}) AND {_short('px.PX_TYPE')} IN ({cpt_types})
                  AND px.PX_DATE IS NOT NULL AND CAST(px.PX_DATE AS date) BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date) THEN 1 ELSE 0 END d1_img,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] px
                WHERE CONVERT(nvarchar(255),px.PATID)=d.patid AND {_norm('px.PX')} IN ({mri}) AND {_short('px.PX_TYPE')} IN ({cpt_types})
                  AND px.PX_DATE IS NOT NULL AND CAST(px.PX_DATE AS date) BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date) THEN 1 ELSE 0 END d3_img,
              CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                WHERE CONVERT(nvarchar(255),l.PATID)=d.patid AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                  AND l.[{selected_lab_date}] IS NOT NULL AND CAST(l.[{selected_lab_date}] AS date) BETWEEN d.admit_date AND d.discharge_date) THEN 1 ELSE 0 END lipid
            INTO #h_src_enc FROM #h_dx d;
            """)

            print("progress: materializing lineage-faithful OMOP evidence", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#h_omop_base') IS NOT NULL DROP TABLE #h_omop_base")
            con.exec_driver_sql(f"""
            SELECT d.patid,d.encounterid,d.diagnosisid,d.dx_date,d.admit_date,d.discharge_date,d.birth_date,
                   p.person_id,v.visit_occurrence_id,CAST(v.visit_start_date AS date) target_admit_date,CAST(v.visit_end_date AS date) target_discharge_date,
                   COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) target_index_date
            INTO #h_omop_base
            FROM #h_dx d
            JOIN [{target_schema}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
            JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
            JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
            JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
            JOIN [{target_schema}].[condition_occurrence] co ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=p.person_id AND co.visit_occurrence_id=v.visit_occurrence_id;
            """)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#h_omop_enc') IS NOT NULL DROP TABLE #h_omop_enc")
            con.exec_driver_sql(f"""
            SELECT b.*,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                JOIN [{target_schema}].[procedure_occurrence] po ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid AND {_norm('sp.PX')} IN ({ct_mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                  AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date) THEN 1 ELSE 0 END d1_img,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                JOIN [{target_schema}].[procedure_occurrence] po ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid AND {_norm('sp.PX')} IN ({mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                  AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date) THEN 1 ELSE 0 END d3_img,
              CASE WHEN EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                JOIN [{target_schema}].[etl_measurement_xwalk] mx ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                JOIN [{target_schema}].[measurement] m ON m.measurement_id=mx.measurement_id AND m.person_id=b.person_id
                WHERE CONVERT(nvarchar(255),l.PATID)=b.patid AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                  AND m.measurement_date BETWEEN b.target_admit_date AND b.target_discharge_date
              ) OR EXISTS (
                SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                JOIN [{target_schema}].[etl_observation_xwalk] ox ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                JOIN [{target_schema}].[observation] o ON o.observation_id=ox.observation_id AND o.person_id=b.person_id
                WHERE CONVERT(nvarchar(255),l.PATID)=b.patid AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                  AND o.observation_date BETWEEN b.target_admit_date AND b.target_discharge_date
              ) THEN 1 ELSE 0 END lipid
            INTO #h_omop_enc FROM #h_omop_base b;
            """)

            def make_source(name: str, where: str) -> None:
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#h_src_{name}') IS NOT NULL DROP TABLE #h_src_{name}")
                con.exec_driver_sql(f"""
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY dx_date,encounterid) rn
                  FROM #h_src_enc WHERE {where}
                )
                SELECT patid,dx_date index_date INTO #h_src_{name}
                FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,dx_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_h_src_{name} ON #h_src_{name}(patid);
                """)

            def make_omop(name: str, where: str) -> None:
                con.exec_driver_sql(f"IF OBJECT_ID('tempdb..#h_omop_{name}') IS NOT NULL DROP TABLE #h_omop_{name}")
                con.exec_driver_sql(f"""
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY target_index_date,encounterid) rn
                  FROM #h_omop_enc WHERE {where}
                )
                SELECT patid,target_index_date index_date INTO #h_omop_{name}
                FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,target_index_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_h_omop_{name} ON #h_omop_{name}(patid);
                """)

            make_source("d0", "1=1")
            make_source("d1", "d1_img=1 AND lipid=1")
            make_source("d3", "d3_img=1 AND lipid=1")
            make_omop("d0", "1=1")
            make_omop("d1", "d1_img=1 AND lipid=1")
            make_omop("d3", "d3_img=1 AND lipid=1")

            results = {name.upper(): _metrics(con, f"#h_src_{name}", f"#h_omop_{name}") for name in ("d0", "d1", "d3")}
    finally:
        engine.dispose()

    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "harmonized_dxdate_sensitivity"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "stage_c_stroke_harmonized_dxdate_sensitivity_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "selected_source_lipid_date_field": selected_lab_date,
        "results": results,
        "interpretation_guardrail": "Post-freeze sensitivity only. The primary Stage C source-faithful analysis remains unchanged; this analysis estimates residual discordance after imposing non-null DX_DATE symmetrically on the source phenotype.",
        "disclosure_review": {"aggregate_only_outputs": True, "patient_identifiers_written": False, "row_level_phi_written": False, "status": "passed"},
    }
    out = out_dir / "stage_c_stroke_harmonized_dxdate_sensitivity.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_c_stroke_harmonized_dxdate_sensitivity_complete")
    for k, v in results.items():
        print(f"{k}: {v}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage C harmonized non-null-DX_DATE sensitivity")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
