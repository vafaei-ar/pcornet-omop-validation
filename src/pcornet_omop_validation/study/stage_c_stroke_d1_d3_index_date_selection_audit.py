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
from pcornet_omop_validation.study.stage_c_stroke_d1_d3_mechanism_audit import (
    FROZEN_ETL_SHA,
    STUDY_DEFINITION,
    LIPID_ARTIFACT,
    CT_CODES,
    MRI_CODES,
    CPT_TYPES,
    LAB_DATE_PRIORITY,
    _columns,
    _load_loincs,
    _norm,
    _short,
    _sql_list,
)
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES


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


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    cfg = load_etl_config(config_path)
    root = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d1_d3"
    root.mkdir(parents=True, exist_ok=True)

    concordance_path = root / "stage_c_stroke_d1_d3_concordance.json"
    mechanism_path = root / "stage_c_stroke_d1_d3_post_outcome_mechanism_audit.json"
    if not concordance_path.exists() or not mechanism_path.exists():
        raise RuntimeError("Completed concordance and post-outcome mechanism audit are required")
    concordance = json.loads(concordance_path.read_text(encoding="utf-8"))
    mechanism = json.loads(mechanism_path.read_text(encoding="utf-8"))
    if concordance.get("status") != "stage_c_stroke_d1_d3_concordance_complete":
        raise RuntimeError("D1/D3 concordance is not complete")
    if mechanism.get("status") != "stage_c_stroke_d1_d3_post_outcome_mechanism_audit_complete":
        raise RuntimeError("D1/D3 mechanism audit is not complete")
    if concordance.get("frozen_etl_sha") != FROZEN_ETL_SHA or mechanism.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Inputs are not anchored to the frozen ETL")
    if concordance.get("study_definition_sha256") != _sha256(STUDY_DEFINITION):
        raise RuntimeError("Study definition differs from completed concordance")

    sql_cfg = cfg.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    lipid_list = _sql_list(_load_loincs())
    all_stroke = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    ct_mri = _sql_list(CT_CODES | MRI_CODES)
    mri = _sql_list(MRI_CODES)
    cpt_types = _sql_list(CPT_TYPES)

    engine = make_engine(cfg)
    results: dict[str, Any] = {}
    try:
        with engine.connect() as con:
            required = [
                (source_schema, "PCORnet_DIAGNOSIS"), (source_schema, "PCORnet_ENCOUNTER"),
                (source_schema, "PCORnet_DEMOGRAPHIC"), (source_schema, "PCORnet_PROCEDURES"),
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (target_schema, "person"), (target_schema, "visit_occurrence"),
                (target_schema, "condition_occurrence"), (target_schema, "procedure_occurrence"),
                (target_schema, "measurement"), (target_schema, "observation"),
                (target_schema, "etl_visit_occurrence_xwalk"), (target_schema, "etl_condition_occurrence_xwalk"),
                (target_schema, "etl_procedure_occurrence_xwalk"), (target_schema, "etl_measurement_xwalk"),
                (target_schema, "etl_observation_xwalk"),
            ]
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Missing required table [{schema}].[{table}]")

            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            selected_lab_date = next((c for c in LAB_DATE_PRIORITY if c in lab_cols), None)
            if selected_lab_date != concordance.get("selected_lipid_date_field"):
                raise RuntimeError("Selected lipid date field differs from completed concordance")

            print("progress: rebuilding locked D1/D3 source cohorts for index-date selection audit", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#d0_candidates') IS NOT NULL DROP TABLE #d0_candidates")
            con.exec_driver_sql(f"""
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255),d.PATID) AS patid,
                         CONVERT(nvarchar(255),d.ENCOUNTERID) AS encounterid,
                         CONVERT(nvarchar(255),d.DIAGNOSISID) AS diagnosisid,
                         CAST(d.DX_DATE AS date) AS dx_date,
                         ROW_NUMBER() OVER (
                           PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                         ) AS rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({all_stroke}) AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
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
                CREATE INDEX IX_dateaudit_d0 ON #d0_candidates(patid,encounterid);
            """)
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
                CREATE INDEX IX_dateaudit_src_enc ON #src_enc(patid,source_index_date,encounterid);
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
                    CREATE UNIQUE CLUSTERED INDEX IX_dateaudit_src_{phenotype} ON #src_{phenotype}(patid);
                """)

            print("progress: rebuilding lineage-faithful D1/D3 OMOP cohorts for index-date selection audit", flush=True)
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
                CREATE INDEX IX_dateaudit_omop_base ON #omop_base(patid,encounterid);
            """)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#omop_enc') IS NOT NULL DROP TABLE #omop_enc")
            con.exec_driver_sql(f"""
                SELECT b.*,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid AND {_norm('sp.PX')} IN ({ct_mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_d1_imaging,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po ON po.procedure_occurrence_id=x.procedure_occurrence_id AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid AND {_norm('sp.PX')} IN ({mri}) AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_d3_imaging,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_measurement_xwalk] mx ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[measurement] m ON m.measurement_id=mx.measurement_id AND m.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                      AND m.measurement_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) OR EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_observation_xwalk] ox ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[observation] o ON o.observation_id=ox.observation_id AND o.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list})
                      AND o.observation_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS target_lipid
                INTO #omop_enc FROM #omop_base b;
                CREATE INDEX IX_dateaudit_omop_enc ON #omop_enc(patid,target_index_date,encounterid);
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
                    FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,target_birth_date,target_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_dateaudit_omop_{phenotype} ON #omop_{phenotype}(patid);
                """)

            print("progress: attributing shared index-date discordance to episode-selection mechanisms", flush=True)
            for phenotype, target_imaging_col in (("d1", "target_d1_imaging"), ("d3", "target_d3_imaging")):
                expected = concordance[phenotype.upper()]["primary_transformation_fidelity"]
                shared_n = int(con.execute(text(f"SELECT COUNT_BIG(*) FROM #src_{phenotype} s JOIN #omop_{phenotype} o ON o.patid=s.patid")).scalar_one())
                mismatch_n = int(con.execute(text(f"SELECT COUNT_BIG(*) FROM #src_{phenotype} s JOIN #omop_{phenotype} o ON o.patid=s.patid WHERE s.index_date<>o.index_date")).scalar_one())
                rows = [dict(r) for r in con.execute(text(f"""
                    WITH m AS (
                      SELECT s.patid,s.encounterid AS source_encounterid,s.diagnosisid,s.dx_date,s.index_date AS source_index_date,
                             o.encounterid AS omop_encounterid,o.index_date AS omop_index_date
                      FROM #src_{phenotype} s JOIN #omop_{phenotype} o ON o.patid=s.patid
                      WHERE s.index_date<>o.index_date
                    )
                    SELECT category,COUNT_BIG(*) AS patients FROM (
                      SELECT m.patid,
                        CASE
                          WHEN m.source_encounterid=m.omop_encounterid THEN 'same_encounter_index_date_representation_difference'
                          WHEN b.patid IS NULL AND m.dx_date IS NULL THEN 'different_episode_source_selected_diagnosis_missing_lineage_with_null_dx_date'
                          WHEN b.patid IS NULL THEN 'different_episode_source_selected_episode_missing_lineage_other'
                          WHEN NOT ({target_imaging_col}=1 AND target_lipid=1 AND target_index_date IS NOT NULL AND target_birth_date IS NOT NULL
                                    AND FLOOR(DATEDIFF(day,target_birth_date,target_index_date)/365.0)>=18)
                            THEN 'different_episode_source_selected_episode_not_target_qualifying'
                          ELSE 'different_episode_both_target_qualifying_ordering_difference'
                        END AS category
                      FROM m
                      LEFT JOIN #omop_enc e ON e.patid=m.patid AND e.encounterid=m.source_encounterid
                      LEFT JOIN #omop_base b ON b.patid=m.patid AND b.encounterid=m.source_encounterid
                    ) q GROUP BY category ORDER BY category;
                """)).mappings().all()]
                for r in rows:
                    r["patients"] = int(r["patients"] or 0)
                exact_n = int(expected["exact_date_patients"])
                checks = {
                    "shared_count_matches_completed_concordance": shared_n == int(expected["intersection_patients"]),
                    "exact_plus_mismatch_closes_shared": exact_n + mismatch_n == shared_n,
                    "category_counts_close_mismatch": sum(r["patients"] for r in rows) == mismatch_n,
                    "mechanism_audit_shared_distribution_closes": sum(int(r["patients"]) for r in mechanism[phenotype.upper()]["shared_index_date_day_difference_distribution"]) == shared_n,
                }
                if not all(checks.values()):
                    raise RuntimeError(f"{phenotype.upper()} index-date audit reproduction checks failed: {checks}")
                results[phenotype.upper()] = {
                    "reproduction_checks": checks,
                    "shared_patients": shared_n,
                    "exact_index_date_patients": exact_n,
                    "mismatched_index_date_patients": mismatch_n,
                    "mismatch_percent_among_shared": 100.0 * mismatch_n / shared_n if shared_n else None,
                    "mechanism_categories": rows,
                }
    finally:
        engine.dispose()

    summary = {
        "status": "stage_c_stroke_d1_d3_post_outcome_index_date_selection_audit_complete",
        "analysis_role": "post_outcome_diagnostic_not_prespecified_primary_estimand",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition_sha256": _sha256(STUDY_DEFINITION),
        "lipid_artifact_sha256": _sha256(LIPID_ARTIFACT),
        "completed_concordance_analysis_git_sha": concordance.get("analysis_git_sha"),
        "audit_analysis_git_sha": _git("rev-parse", "HEAD"),
        "audit_worktree_clean": _git("status", "--porcelain") == "",
        "selected_lipid_date_field": concordance.get("selected_lipid_date_field"),
        "D1": results["D1"],
        "D3": results["D3"],
        "interpretation_guardrail": "This post-outcome audit explains selected-index-date discordance only. It does not alter the frozen ETL, phenotype definitions, primary estimand, or completed concordance outputs.",
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "source_record_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }
    out_json = root / "stage_c_stroke_d1_d3_post_outcome_index_date_selection_audit.json"
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    csv_rows: list[dict[str, object]] = []
    for phenotype in ("D1", "D3"):
        for row in summary[phenotype]["mechanism_categories"]:
            csv_rows.append({"phenotype": phenotype, **row})
    _write_csv(root / "stage_c_stroke_d1_d3_post_outcome_index_date_selection_mechanisms.csv", csv_rows)

    print("status: stage_c_stroke_d1_d3_post_outcome_index_date_selection_audit_complete")
    print(f"audit_analysis_git_sha: {summary['audit_analysis_git_sha']}")
    for phenotype in ("D1", "D3"):
        r = summary[phenotype]
        print(f"{phenotype}_shared_patients: {r['shared_patients']}")
        print(f"{phenotype}_exact_index_date_patients: {r['exact_index_date_patients']}")
        print(f"{phenotype}_mismatched_index_date_patients: {r['mismatched_index_date_patients']}")
        print(f"{phenotype}_mismatch_percent_among_shared: {r['mismatch_percent_among_shared']}")
        for item in r["mechanism_categories"]:
            print(f"{phenotype}_{item['category']}: {item['patients']}")
    print(f"output: {out_json}")
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="Post-outcome shared index-date selection audit for Stage C stroke D1/D3")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
