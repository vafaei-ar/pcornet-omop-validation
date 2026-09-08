from __future__ import annotations

"""Set-based implementation of the Stage C harmonization transition audit.

This module is scientifically identical in purpose to
``stage_c_harmonization_transition_audit`` but avoids repeated correlated probes of
large procedure, measurement, and observation tables. Relevant source evidence is
materialized once, then mapped through the frozen ETL lineage tables. The frozen ETL,
locked phenotype definitions, anchor counts, and interpretation are unchanged.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.stage_c_harmonization_transition_audit import (
    AUDIT_PATH,
    D0_PATH,
    D1D3_PATH,
    EXPECTED_SOURCE_HARMONIZED,
    EXPECTED_SOURCE_PRIMARY,
    EXPECTED_TARGET_HARMONIZED,
    EXPECTED_TARGET_PRIMARY,
    FROZEN_ETL_SHA,
    HARMONIZED_PATH,
    LAB_DATE_PRIORITY,
    LIPID_ARTIFACT,
    CT_CODES,
    MRI_CODES,
    CPT_TYPES,
    _columns,
    _d0_mechanism_bridge,
    _git,
    _load_loincs,
    _norm,
    _scalar,
    _schema,
    _sha256,
    _short,
    _sql_list,
    _transition,
)
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    if (
        audit.get("status")
        != "post_result_descriptive_audit_defined_after_primary_and_harmonized_stage_c_results"
    ):
        raise RuntimeError("Unexpected harmonization transition audit status")
    if audit.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Audit definition is not anchored to the frozen ETL")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    all_stroke = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
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
        / "harmonization_transition_audit"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            lab_cols = _columns(con, source_schema, "PCORnet_LAB_RESULT_CM")
            selected_lab_date = next(
                (column for column in LAB_DATE_PRIORITY if column in lab_cols), None
            )
            if selected_lab_date is None:
                raise RuntimeError("No prespecified lipid date field is available")

            print("progress: materializing shared source candidate episodes", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_candidates') IS NOT NULL DROP TABLE #audit_candidates"
            )
            con.exec_driver_sql(
                f"""
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255),d.PATID) AS patid,
                         CONVERT(nvarchar(255),d.ENCOUNTERID) AS encounterid,
                         CONVERT(nvarchar(255),d.DIAGNOSISID) AS diagnosisid,
                         CAST(d.DX_DATE AS date) AS dx_date,
                         ROW_NUMBER() OVER (
                           PARTITION BY CONVERT(nvarchar(255),d.PATID),
                                        CONVERT(nvarchar(255),d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                                    CAST(d.DX_DATE AS date),
                                    {_norm('d.DX')},
                                    CONVERT(nvarchar(255),d.DIAGNOSISID)
                         ) AS rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({all_stroke})
                    AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
                       CAST(e.ADMIT_DATE AS date) AS admit_date,
                       CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                       COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) AS source_index_date,
                       CAST(dm.BIRTH_DATE AS date) AS source_birth_date
                INTO #audit_candidates
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
                CREATE INDEX IX_audit_candidates
                  ON #audit_candidates(patid,source_index_date,encounterid);
                CREATE INDEX IX_audit_candidates_episode
                  ON #audit_candidates(patid,encounterid,diagnosisid);
                """
            )

            print("progress: materializing relevant source procedure evidence once", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_proc_hits') IS NOT NULL DROP TABLE #audit_proc_hits"
            )
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.admit_date,c.discharge_date,
                       LTRIM(RTRIM(CONVERT(nvarchar(255),px.PROCEDURESID))) AS source_procedure_id,
                       CAST(px.PX_DATE AS date) AS source_px_date,
                       CASE WHEN {_norm('px.PX')} IN ({ct_mri}) THEN 1 ELSE 0 END AS d1_code,
                       CASE WHEN {_norm('px.PX')} IN ({mri}) THEN 1 ELSE 0 END AS d3_code
                INTO #audit_proc_hits
                FROM #audit_candidates c
                JOIN [{source_schema}].[PCORnet_PROCEDURES] px
                  ON CONVERT(nvarchar(255),px.PATID)=c.patid
                WHERE {_norm('px.PX')} IN ({ct_mri})
                  AND {_short('px.PX_TYPE')} IN ({cpt_types});
                CREATE INDEX IX_audit_proc_hits_source
                  ON #audit_proc_hits(source_procedure_id);
                CREATE INDEX IX_audit_proc_hits_episode
                  ON #audit_proc_hits(patid,encounterid,diagnosisid);
                """
            )

            print("progress: materializing relevant source lipid evidence once", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_lab_hits') IS NOT NULL DROP TABLE #audit_lab_hits"
            )
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.admit_date,c.discharge_date,
                       LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID))) AS source_record_id,
                       CAST(l.[{selected_lab_date}] AS date) AS source_lab_date
                INTO #audit_lab_hits
                FROM #audit_candidates c
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON CONVERT(nvarchar(255),l.PATID)=c.patid
                WHERE UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))) IN ({lipid_list});
                CREATE INDEX IX_audit_lab_hits_source
                  ON #audit_lab_hits(source_record_id);
                CREATE INDEX IX_audit_lab_hits_episode
                  ON #audit_lab_hits(patid,encounterid,diagnosisid);
                """
            )

            print("progress: deriving source imaging and lipid flags", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_source_proc_flags') IS NOT NULL DROP TABLE #audit_source_proc_flags"
            )
            con.exec_driver_sql(
                """
                SELECT patid,encounterid,diagnosisid,
                       MAX(CASE WHEN source_px_date IS NOT NULL
                                     AND source_px_date BETWEEN DATEADD(day,-2,admit_date) AND discharge_date
                                THEN d1_code ELSE 0 END) AS d1_img,
                       MAX(CASE WHEN source_px_date IS NOT NULL
                                     AND source_px_date BETWEEN DATEADD(day,-2,admit_date) AND discharge_date
                                THEN d3_code ELSE 0 END) AS d3_img
                INTO #audit_source_proc_flags
                FROM #audit_proc_hits
                GROUP BY patid,encounterid,diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_source_proc_flags
                  ON #audit_source_proc_flags(patid,encounterid,diagnosisid);
                """
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_source_lipid_flags') IS NOT NULL DROP TABLE #audit_source_lipid_flags"
            )
            con.exec_driver_sql(
                """
                SELECT patid,encounterid,diagnosisid,
                       MAX(CASE WHEN source_lab_date IS NOT NULL
                                     AND source_lab_date BETWEEN admit_date AND discharge_date
                                THEN 1 ELSE 0 END) AS lipid
                INTO #audit_source_lipid_flags
                FROM #audit_lab_hits
                GROUP BY patid,encounterid,diagnosisid;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_source_lipid_flags
                  ON #audit_source_lipid_flags(patid,encounterid,diagnosisid);
                """
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_source_enc') IS NOT NULL DROP TABLE #audit_source_enc"
            )
            con.exec_driver_sql(
                """
                SELECT c.*,
                       COALESCE(p.d1_img,0) AS d1_img,
                       COALESCE(p.d3_img,0) AS d3_img,
                       COALESCE(l.lipid,0) AS lipid
                INTO #audit_source_enc
                FROM #audit_candidates c
                LEFT JOIN #audit_source_proc_flags p
                  ON p.patid=c.patid AND p.encounterid=c.encounterid AND p.diagnosisid=c.diagnosisid
                LEFT JOIN #audit_source_lipid_flags l
                  ON l.patid=c.patid AND l.encounterid=c.encounterid AND l.diagnosisid=c.diagnosisid;
                CREATE INDEX IX_audit_source_enc
                  ON #audit_source_enc(patid,source_index_date,encounterid);
                """
            )

            def make_source(phenotype: str, evidence_where: str) -> None:
                for mode, date_filter, index_expr in (
                    ("primary", "1=1", "source_index_date"),
                    ("harmonized", "dx_date IS NOT NULL", "dx_date"),
                ):
                    table = f"#src_{mode}_{phenotype}"
                    con.exec_driver_sql(
                        f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}"
                    )
                    con.exec_driver_sql(
                        f"""
                        ;WITH q AS (
                          SELECT *,ROW_NUMBER() OVER (
                            PARTITION BY patid ORDER BY {index_expr},encounterid
                          ) AS rn
                          FROM #audit_source_enc
                          WHERE {evidence_where} AND {date_filter}
                        )
                        SELECT patid,encounterid,diagnosisid,dx_date,{index_expr} AS index_date
                        INTO {table}
                        FROM q
                        WHERE rn=1
                          AND FLOOR(DATEDIFF(day,source_birth_date,{index_expr})/365.0)>=18;
                        CREATE UNIQUE CLUSTERED INDEX IX_{mode}_{phenotype}_src ON {table}(patid);
                        """
                    )

            make_source("d0", "1=1")
            make_source("d1", "d1_img=1 AND lipid=1")
            make_source("d3", "d3_img=1 AND lipid=1")

            print("progress: materializing OMOP lineage base", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_base') IS NOT NULL DROP TABLE #audit_target_base"
            )
            con.exec_driver_sql(
                f"""
                SELECT d.*,
                       p.person_id,
                       CAST(p.birth_datetime AS date) AS target_birth_date,
                       v.visit_occurrence_id,
                       CAST(v.visit_start_date AS date) AS target_admit_date,
                       CAST(v.visit_end_date AS date) AS target_discharge_date,
                       COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) AS target_index_date,
                       co.condition_occurrence_id
                INTO #audit_target_base
                FROM #audit_candidates d
                JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                  ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                  ON cx.source_domain='DIAGNOSIS'
                 AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id=cx.condition_occurrence_id
                 AND co.person_id=p.person_id
                 AND co.visit_occurrence_id=v.visit_occurrence_id;
                CREATE INDEX IX_audit_target_base_episode
                  ON #audit_target_base(patid,encounterid,diagnosisid,condition_occurrence_id);
                CREATE INDEX IX_audit_target_base_person
                  ON #audit_target_base(person_id,target_index_date);
                """
            )

            print("progress: mapping procedure evidence through frozen lineage", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_proc_flags') IS NOT NULL DROP TABLE #audit_target_proc_flags"
            )
            con.exec_driver_sql(
                f"""
                SELECT b.patid,b.encounterid,b.diagnosisid,b.condition_occurrence_id,
                       MAX(h.d1_code) AS d1_img,
                       MAX(h.d3_code) AS d3_img
                INTO #audit_target_proc_flags
                FROM #audit_target_base b
                JOIN #audit_proc_hits h
                  ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                  ON x.source_procedure_id=h.source_procedure_id
                JOIN [{target_schema}].[procedure_occurrence] po
                  ON po.procedure_occurrence_id=x.procedure_occurrence_id
                 AND po.person_id=b.person_id
                WHERE po.procedure_date BETWEEN DATEADD(day,-2,b.target_admit_date) AND b.target_discharge_date
                GROUP BY b.patid,b.encounterid,b.diagnosisid,b.condition_occurrence_id;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_target_proc_flags
                  ON #audit_target_proc_flags(patid,encounterid,diagnosisid,condition_occurrence_id);
                """
            )

            print("progress: mapping lipid evidence through frozen lineage", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_lipid_flags') IS NOT NULL DROP TABLE #audit_target_lipid_flags"
            )
            con.exec_driver_sql(
                f"""
                ;WITH mapped AS (
                  SELECT b.patid,b.encounterid,b.diagnosisid,b.condition_occurrence_id
                  FROM #audit_target_base b
                  JOIN #audit_lab_hits h
                    ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                  JOIN [{target_schema}].[etl_measurement_xwalk] mx
                    ON mx.source_family='LAB_RESULT_CM' AND mx.source_record_id=h.source_record_id
                  JOIN [{target_schema}].[measurement] m
                    ON m.measurement_id=mx.measurement_id AND m.person_id=b.person_id
                  WHERE m.measurement_date BETWEEN b.target_admit_date AND b.target_discharge_date
                  UNION ALL
                  SELECT b.patid,b.encounterid,b.diagnosisid,b.condition_occurrence_id
                  FROM #audit_target_base b
                  JOIN #audit_lab_hits h
                    ON h.patid=b.patid AND h.encounterid=b.encounterid AND h.diagnosisid=b.diagnosisid
                  JOIN [{target_schema}].[etl_observation_xwalk] ox
                    ON ox.source_family='LAB_RESULT_CM' AND ox.source_record_id=h.source_record_id
                  JOIN [{target_schema}].[observation] o
                    ON o.observation_id=ox.observation_id AND o.person_id=b.person_id
                  WHERE o.observation_date BETWEEN b.target_admit_date AND b.target_discharge_date
                )
                SELECT patid,encounterid,diagnosisid,condition_occurrence_id,1 AS lipid
                INTO #audit_target_lipid_flags
                FROM mapped
                GROUP BY patid,encounterid,diagnosisid,condition_occurrence_id;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_target_lipid_flags
                  ON #audit_target_lipid_flags(patid,encounterid,diagnosisid,condition_occurrence_id);
                """
            )

            print("progress: assembling OMOP episode evidence", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_enc') IS NOT NULL DROP TABLE #audit_target_enc"
            )
            con.exec_driver_sql(
                """
                SELECT b.*,
                       COALESCE(p.d1_img,0) AS d1_img,
                       COALESCE(p.d3_img,0) AS d3_img,
                       COALESCE(l.lipid,0) AS lipid
                INTO #audit_target_enc
                FROM #audit_target_base b
                LEFT JOIN #audit_target_proc_flags p
                  ON p.patid=b.patid AND p.encounterid=b.encounterid
                 AND p.diagnosisid=b.diagnosisid
                 AND p.condition_occurrence_id=b.condition_occurrence_id
                LEFT JOIN #audit_target_lipid_flags l
                  ON l.patid=b.patid AND l.encounterid=b.encounterid
                 AND l.diagnosisid=b.diagnosisid
                 AND l.condition_occurrence_id=b.condition_occurrence_id;
                CREATE INDEX IX_audit_target_enc
                  ON #audit_target_enc(patid,target_index_date,encounterid);
                """
            )

            print("progress: materializing primary and harmonized OMOP cohorts", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#target_primary_d0') IS NOT NULL DROP TABLE #target_primary_d0"
            )
            con.exec_driver_sql(
                """
                ;WITH x AS (
                  SELECT b.patid,b.encounterid,b.diagnosisid,b.dx_date,
                         b.target_index_date AS index_date,b.condition_occurrence_id,
                         ROW_NUMBER() OVER (
                           PARTITION BY b.patid ORDER BY b.condition_occurrence_id
                         ) AS rn
                  FROM #src_primary_d0 s
                  JOIN #audit_target_base b
                    ON b.patid=s.patid AND b.encounterid=s.encounterid AND b.diagnosisid=s.diagnosisid
                )
                SELECT patid,encounterid,diagnosisid,dx_date,index_date
                INTO #target_primary_d0
                FROM x WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_target_primary_d0 ON #target_primary_d0(patid);
                """
            )

            def make_target(mode: str, phenotype: str, evidence_where: str, birth_expr: str) -> None:
                table = f"#target_{mode}_{phenotype}"
                mode_filter = "1=1" if mode == "primary" else "dx_date IS NOT NULL"
                con.exec_driver_sql(
                    f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}"
                )
                con.exec_driver_sql(
                    f"""
                    ;WITH q AS (
                      SELECT *,ROW_NUMBER() OVER (
                        PARTITION BY patid
                        ORDER BY target_index_date,encounterid,diagnosisid,condition_occurrence_id
                      ) AS rn
                      FROM #audit_target_enc
                      WHERE {evidence_where} AND {mode_filter}
                    )
                    SELECT patid,encounterid,diagnosisid,dx_date,target_index_date AS index_date
                    INTO {table}
                    FROM q
                    WHERE rn=1
                      AND FLOOR(DATEDIFF(day,{birth_expr},target_index_date)/365.0)>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_target_{mode}_{phenotype} ON {table}(patid);
                    """
                )

            make_target("harmonized", "d0", "1=1", "source_birth_date")
            for phenotype, evidence in (
                ("d1", "d1_img=1 AND lipid=1"),
                ("d3", "d3_img=1 AND lipid=1"),
            ):
                make_target("primary", phenotype, evidence, "target_birth_date")
                make_target("harmonized", phenotype, evidence, "source_birth_date")

            source_results: dict[str, Any] = {}
            target_results: dict[str, Any] = {}
            for phenotype in ("D0", "D1", "D3"):
                key = phenotype.lower()
                source_results[phenotype] = _transition(
                    con, f"#src_primary_{key}", f"#src_harmonized_{key}"
                )
                target_results[phenotype] = _transition(
                    con, f"#target_primary_{key}", f"#target_harmonized_{key}"
                )

                if source_results[phenotype]["primary_patients"] != EXPECTED_SOURCE_PRIMARY[phenotype]:
                    raise RuntimeError(f"{phenotype} primary source anchor mismatch: {source_results[phenotype]}")
                if source_results[phenotype]["harmonized_patients"] != EXPECTED_SOURCE_HARMONIZED[phenotype]:
                    raise RuntimeError(f"{phenotype} harmonized source anchor mismatch: {source_results[phenotype]}")
                if target_results[phenotype]["primary_patients"] != EXPECTED_TARGET_PRIMARY[phenotype]:
                    raise RuntimeError(f"{phenotype} primary OMOP anchor mismatch: {target_results[phenotype]}")
                if target_results[phenotype]["harmonized_patients"] != EXPECTED_TARGET_HARMONIZED[phenotype]:
                    raise RuntimeError(f"{phenotype} harmonized OMOP anchor mismatch: {target_results[phenotype]}")

            d0_bridge = _d0_mechanism_bridge(con)
            d0_bridge["primary_source_only_not_harmonized"] = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM #src_primary_d0 p
                LEFT JOIN #src_harmonized_d0 h ON h.patid=p.patid
                WHERE h.patid IS NULL
                """,
            )
            d0_bridge["primary_source_only_reselected_into_harmonized"] = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM #src_primary_d0 p
                JOIN #src_harmonized_d0 h ON h.patid=p.patid
                WHERE p.dx_date IS NULL
                  AND (p.encounterid<>h.encounterid OR p.diagnosisid<>h.diagnosisid)
                """,
            )
    finally:
        engine.dispose()

    payload: dict[str, object] = {
        "status": "stage_c_harmonization_transition_audit_complete",
        "audit_role": "post_result_descriptive_mechanism_audit",
        "implementation": "set_based_evidence_materialization_v2",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "audit_definition": str(AUDIT_PATH),
        "audit_definition_sha256": _sha256(AUDIT_PATH),
        "primary_d0_definition_sha256": _sha256(D0_PATH),
        "primary_d1_d3_definition_sha256": _sha256(D1D3_PATH),
        "harmonized_definition_sha256": _sha256(HARMONIZED_PATH),
        "selected_source_lipid_date_field": selected_lab_date,
        "source_episode_transitions": source_results,
        "omop_episode_transitions": target_results,
        "d0_mechanism_bridge": d0_bridge,
        "interpretation_guardrail": (
            "This audit quantifies patient and episode reselection after applying the "
            "recorded-diagnosis-date requirement before episode selection. It does not "
            "replace the source-faithful primary Stage C analysis or modify the frozen ETL."
        ),
        "performance_note": (
            "Relevant procedure and lipid evidence was materialized once and then mapped "
            "through the frozen lineage tables; this changes query execution only, not "
            "the phenotype or evidence rules."
        ),
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    output_path = out_dir / "stage_c_harmonization_transition_audit.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("status:", payload["status"])
    print("source episode transitions:")
    print(json.dumps(source_results, indent=2, sort_keys=True))
    print("OMOP episode transitions:")
    print(json.dumps(target_results, indent=2, sort_keys=True))
    print("D0 mechanism bridge:")
    print(json.dumps(d0_bridge, indent=2, sort_keys=True))
    print("output:", output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set-based Stage C patient and episode transition audit"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
