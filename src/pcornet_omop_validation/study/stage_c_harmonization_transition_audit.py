from __future__ import annotations

"""Post-result audit of Stage C episode reselection under diagnosis-date harmonization.

This module does not alter the frozen ETL or replace the locked Stage C analyses. It
reconstructs, in one database session, the source-faithful and harmonized D0/D1/D3
cohorts and quantifies exactly which patients and selected episodes change when the
recorded-diagnosis-date requirement is applied before patient-level episode selection.
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
AUDIT_PATH = Path("study_definitions/stage_c_harmonization_transition_audit_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
D1D3_PATH = Path("study_definitions/stage_c_stroke_d1_d3_v1.json")
HARMONIZED_PATH = Path("study_definitions/stage_c_stroke_harmonized_dxdate_sensitivity_v1.json")
LIPID_ARTIFACT = Path("study_definitions/artifacts/stage_c_lipid_loinc_whitelist_v1.csv")

CT_CODES = frozenset({"70450", "70460", "70470"})
MRI_CODES = frozenset({"70551", "70552", "70553", "70557", "70558", "70559"})
CPT_TYPES = frozenset({"CH", "CPT", "CPT4", "HCPCS"})
LAB_DATE_PRIORITY = ("LAB_TKN_DTTM", "SPECIMEN_DATE", "LAB_DATE", "RESULT_DATE")

EXPECTED_SOURCE_PRIMARY = {"D0": 9815, "D1": 8624, "D3": 7565}
EXPECTED_SOURCE_HARMONIZED = {"D0": 6198, "D1": 5246, "D3": 4710}
EXPECTED_TARGET_PRIMARY = {"D0": 6001, "D1": 5246, "D3": 4710}
EXPECTED_TARGET_HARMONIZED = {"D0": 6198, "D1": 5246, "D3": 4710}


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema(value: object) -> str:
    schema = str(value or "dbo")
    if not schema.replace("_", "a").isalnum() or schema[0].isdigit():
        raise ValueError(f"Unsafe schema: {schema!r}")
    return schema


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values) -> str:
    return ",".join(
        "'" + str(value).replace("'", "''") + "'" for value in sorted(values)
    )


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(
        text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"
        ),
        {"s": schema, "t": table},
    ).fetchall()
    return {str(row[0]).upper() for row in rows}


def _load_loincs() -> list[str]:
    with LIPID_ARTIFACT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return sorted(
            {
                str(row.get("LOINC_NUM") or "").strip().upper()
                for row in reader
                if str(row.get("LOINC_NUM") or "").strip()
            }
        )


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _transition(con, primary_table: str, harmonized_table: str) -> dict[str, Any]:
    row = con.execute(
        text(
            f"""
            WITH p AS (
              SELECT patid, encounterid, diagnosisid, dx_date, index_date
              FROM {primary_table}
            ),
            h AS (
              SELECT patid, encounterid, diagnosisid, dx_date, index_date
              FROM {harmonized_table}
            ),
            u AS (
              SELECT patid FROM p
              UNION
              SELECT patid FROM h
            )
            SELECT
              (SELECT COUNT_BIG(*) FROM p) AS primary_patients,
              (SELECT COUNT_BIG(*) FROM h) AS harmonized_patients,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL THEN 1 ELSE 0 END) AS shared_patients,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NULL THEN 1 ELSE 0 END) AS primary_only_patients,
              SUM(CASE WHEN p.patid IS NULL AND h.patid IS NOT NULL THEN 1 ELSE 0 END) AS harmonized_only_patients,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND p.encounterid=h.encounterid AND p.diagnosisid=h.diagnosisid
                       THEN 1 ELSE 0 END) AS same_selected_episode,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND (p.encounterid<>h.encounterid OR p.diagnosisid<>h.diagnosisid)
                       THEN 1 ELSE 0 END) AS different_reselected_episode,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND p.index_date=h.index_date
                       THEN 1 ELSE 0 END) AS same_index_date,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND p.index_date<>h.index_date
                       THEN 1 ELSE 0 END) AS changed_index_date,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND h.index_date>p.index_date
                       THEN 1 ELSE 0 END) AS harmonized_index_later,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND h.index_date<p.index_date
                       THEN 1 ELSE 0 END) AS harmonized_index_earlier,
              SUM(CASE WHEN p.patid IS NOT NULL AND p.dx_date IS NULL THEN 1 ELSE 0 END) AS primary_selected_diagnosis_date_missing,
              SUM(CASE WHEN p.patid IS NOT NULL AND p.dx_date IS NOT NULL THEN 1 ELSE 0 END) AS primary_selected_diagnosis_date_recorded,
              SUM(CASE WHEN h.patid IS NOT NULL AND h.dx_date IS NULL THEN 1 ELSE 0 END) AS harmonized_selected_diagnosis_date_missing,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND p.dx_date IS NULL
                       THEN 1 ELSE 0 END) AS shared_with_primary_selected_diagnosis_date_missing,
              SUM(CASE WHEN p.patid IS NOT NULL AND h.patid IS NOT NULL
                        AND (p.encounterid<>h.encounterid OR p.diagnosisid<>h.diagnosisid)
                        AND p.dx_date IS NULL
                       THEN 1 ELSE 0 END) AS reselected_with_primary_selected_diagnosis_date_missing
            FROM u
            LEFT JOIN p ON p.patid=u.patid
            LEFT JOIN h ON h.patid=u.patid
            """
        )
    ).mappings().one()
    result = {key: int(value or 0) for key, value in dict(row).items()}
    result["net_patient_change"] = (
        result["harmonized_patients"] - result["primary_patients"]
    )
    return result


def _d0_mechanism_bridge(con) -> dict[str, int]:
    row = con.execute(
        text(
            """
            WITH entered AS (
              SELECT h.patid
              FROM #target_harmonized_d0 h
              LEFT JOIN #target_primary_d0 p ON p.patid=h.patid
              WHERE p.patid IS NULL
            )
            SELECT
              COUNT_BIG(*) AS entered_harmonized_target_d0,
              SUM(CASE WHEN sp.patid IS NOT NULL THEN 1 ELSE 0 END) AS entered_present_in_primary_source_d0,
              SUM(CASE WHEN sp.dx_date IS NULL THEN 1 ELSE 0 END) AS entered_primary_source_selected_diagnosis_date_missing,
              SUM(CASE WHEN sp.dx_date IS NOT NULL THEN 1 ELSE 0 END) AS entered_primary_source_selected_diagnosis_date_recorded,
              SUM(CASE WHEN sh.patid IS NOT NULL THEN 1 ELSE 0 END) AS entered_present_in_harmonized_source_d0,
              SUM(CASE WHEN sp.patid IS NOT NULL AND sh.patid IS NOT NULL
                        AND (sp.encounterid<>sh.encounterid OR sp.diagnosisid<>sh.diagnosisid)
                       THEN 1 ELSE 0 END) AS entered_reselected_to_different_source_episode,
              SUM(CASE WHEN sp.patid IS NOT NULL AND sh.patid IS NOT NULL
                        AND sh.index_date>sp.index_date
                       THEN 1 ELSE 0 END) AS entered_harmonized_source_episode_later,
              SUM(CASE WHEN sp.patid IS NOT NULL AND sh.patid IS NOT NULL
                        AND sh.index_date=sp.index_date
                       THEN 1 ELSE 0 END) AS entered_same_source_index_date,
              SUM(CASE WHEN sh.dx_date IS NOT NULL THEN 1 ELSE 0 END) AS entered_harmonized_selected_diagnosis_date_recorded
            FROM entered e
            LEFT JOIN #src_primary_d0 sp ON sp.patid=e.patid
            LEFT JOIN #src_harmonized_d0 sh ON sh.patid=e.patid
            """
        )
    ).mappings().one()
    return {key: int(value or 0) for key, value in dict(row).items()}


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
                "IF OBJECT_ID('tempdb..#audit_candidates') IS NOT NULL "
                "DROP TABLE #audit_candidates"
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
                SELECT x.patid,
                       x.encounterid,
                       x.diagnosisid,
                       x.dx_date,
                       CAST(e.ADMIT_DATE AS date) AS admit_date,
                       CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                       COALESCE(
                         x.dx_date,
                         CAST(e.ADMIT_DATE AS date),
                         CAST(e.DISCHARGE_DATE AS date)
                       ) AS source_index_date,
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
                  AND DATEDIFF(
                        day,
                        CAST(e.ADMIT_DATE AS date),
                        CAST(e.DISCHARGE_DATE AS date)
                      )>=1
                  AND COALESCE(
                        x.dx_date,
                        CAST(e.ADMIT_DATE AS date),
                        CAST(e.DISCHARGE_DATE AS date)
                      ) IS NOT NULL;
                CREATE INDEX IX_audit_candidates
                  ON #audit_candidates(patid,source_index_date,encounterid);
                """
            )

            print("progress: materializing source imaging and lipid evidence", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_source_enc') IS NOT NULL "
                "DROP TABLE #audit_source_enc"
            )
            con.exec_driver_sql(
                f"""
                SELECT d.*,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] px
                    WHERE CONVERT(nvarchar(255),px.PATID)=d.patid
                      AND {_norm('px.PX')} IN ({ct_mri})
                      AND {_short('px.PX_TYPE')} IN ({cpt_types})
                      AND px.PX_DATE IS NOT NULL
                      AND CAST(px.PX_DATE AS date)
                          BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date
                  ) THEN 1 ELSE 0 END AS d1_img,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] px
                    WHERE CONVERT(nvarchar(255),px.PATID)=d.patid
                      AND {_norm('px.PX')} IN ({mri})
                      AND {_short('px.PX_TYPE')} IN ({cpt_types})
                      AND px.PX_DATE IS NOT NULL
                      AND CAST(px.PX_DATE AS date)
                          BETWEEN DATEADD(day,-2,d.admit_date) AND d.discharge_date
                  ) THEN 1 ELSE 0 END AS d3_img,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    WHERE CONVERT(nvarchar(255),l.PATID)=d.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC))))
                          IN ({lipid_list})
                      AND l.[{selected_lab_date}] IS NOT NULL
                      AND CAST(l.[{selected_lab_date}] AS date)
                          BETWEEN d.admit_date AND d.discharge_date
                  ) THEN 1 ELSE 0 END AS lipid
                INTO #audit_source_enc
                FROM #audit_candidates d;
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
                          SELECT *,
                                 ROW_NUMBER() OVER (
                                   PARTITION BY patid
                                   ORDER BY {index_expr},encounterid
                                 ) AS rn
                          FROM #audit_source_enc
                          WHERE {evidence_where} AND {date_filter}
                        )
                        SELECT patid,
                               encounterid,
                               diagnosisid,
                               dx_date,
                               {index_expr} AS index_date
                        INTO {table}
                        FROM q
                        WHERE rn=1
                          AND FLOOR(
                                DATEDIFF(day,source_birth_date,{index_expr})/365.0
                              )>=18;
                        CREATE UNIQUE CLUSTERED INDEX IX_{mode}_{phenotype}_src
                          ON {table}(patid);
                        """
                    )

            make_source("d0", "1=1")
            make_source("d1", "d1_img=1 AND lipid=1")
            make_source("d3", "d3_img=1 AND lipid=1")

            print("progress: materializing OMOP lineage and target evidence", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_base') IS NOT NULL "
                "DROP TABLE #audit_target_base"
            )
            con.exec_driver_sql(
                f"""
                SELECT d.*,
                       p.person_id,
                       CAST(p.birth_datetime AS date) AS target_birth_date,
                       v.visit_occurrence_id,
                       CAST(v.visit_start_date AS date) AS target_admit_date,
                       CAST(v.visit_end_date AS date) AS target_discharge_date,
                       COALESCE(
                         CAST(co.condition_start_date AS date),
                         CAST(v.visit_start_date AS date),
                         CAST(v.visit_end_date AS date)
                       ) AS target_index_date,
                       co.condition_occurrence_id
                INTO #audit_target_base
                FROM #audit_candidates d
                JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                  ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id=vx.visit_occurrence_id
                 AND v.person_id=p.person_id
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                  ON cx.source_domain='DIAGNOSIS'
                 AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id=cx.condition_occurrence_id
                 AND co.person_id=p.person_id
                 AND co.visit_occurrence_id=v.visit_occurrence_id;
                CREATE INDEX IX_audit_target_base
                  ON #audit_target_base(patid,target_index_date,encounterid);
                """
            )

            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_target_enc') IS NOT NULL "
                "DROP TABLE #audit_target_enc"
            )
            con.exec_driver_sql(
                f"""
                SELECT b.*,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                      ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po
                      ON po.procedure_occurrence_id=x.procedure_occurrence_id
                     AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid
                      AND {_norm('sp.PX')} IN ({ct_mri})
                      AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date
                          BETWEEN DATEADD(day,-2,b.target_admit_date)
                              AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS d1_img,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_PROCEDURES] sp
                    JOIN [{target_schema}].[etl_procedure_occurrence_xwalk] x
                      ON x.source_procedure_id=LTRIM(RTRIM(CONVERT(nvarchar(255),sp.PROCEDURESID)))
                    JOIN [{target_schema}].[procedure_occurrence] po
                      ON po.procedure_occurrence_id=x.procedure_occurrence_id
                     AND po.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),sp.PATID)=b.patid
                      AND {_norm('sp.PX')} IN ({mri})
                      AND {_short('sp.PX_TYPE')} IN ({cpt_types})
                      AND po.procedure_date
                          BETWEEN DATEADD(day,-2,b.target_admit_date)
                              AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS d3_img,
                  CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_measurement_xwalk] mx
                      ON mx.source_family='LAB_RESULT_CM'
                     AND mx.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[measurement] m
                      ON m.measurement_id=mx.measurement_id
                     AND m.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC))))
                          IN ({lipid_list})
                      AND m.measurement_date
                          BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) OR EXISTS (
                    SELECT 1
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[etl_observation_xwalk] ox
                      ON ox.source_family='LAB_RESULT_CM'
                     AND ox.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),l.LAB_RESULT_CM_ID)))
                    JOIN [{target_schema}].[observation] o
                      ON o.observation_id=ox.observation_id
                     AND o.person_id=b.person_id
                    WHERE CONVERT(nvarchar(255),l.PATID)=b.patid
                      AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC))))
                          IN ({lipid_list})
                      AND o.observation_date
                          BETWEEN b.target_admit_date AND b.target_discharge_date
                  ) THEN 1 ELSE 0 END AS lipid
                INTO #audit_target_enc
                FROM #audit_target_base b;
                CREATE INDEX IX_audit_target_enc
                  ON #audit_target_enc(patid,target_index_date,encounterid);
                """
            )

            print("progress: materializing primary and harmonized OMOP cohorts", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#target_primary_d0') IS NOT NULL "
                "DROP TABLE #target_primary_d0"
            )
            con.exec_driver_sql(
                f"""
                ;WITH x AS (
                  SELECT s.patid,
                         s.encounterid,
                         s.diagnosisid,
                         s.dx_date,
                         COALESCE(
                           CAST(co.condition_start_date AS date),
                           CAST(v.visit_start_date AS date),
                           CAST(v.visit_end_date AS date)
                         ) AS index_date,
                         co.condition_occurrence_id,
                         ROW_NUMBER() OVER (
                           PARTITION BY s.patid
                           ORDER BY co.condition_occurrence_id
                         ) AS rn
                  FROM #src_primary_d0 s
                  JOIN [{target_schema}].[person] p
                    ON CONVERT(nvarchar(255),p.person_source_value)=s.patid
                  JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                    ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
                  JOIN [{target_schema}].[visit_occurrence] v
                    ON v.visit_occurrence_id=vx.visit_occurrence_id
                   AND v.person_id=p.person_id
                  JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                    ON cx.source_domain='DIAGNOSIS'
                   AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
                  JOIN [{target_schema}].[condition_occurrence] co
                    ON co.condition_occurrence_id=cx.condition_occurrence_id
                   AND co.person_id=p.person_id
                   AND co.visit_occurrence_id=v.visit_occurrence_id
                )
                SELECT patid,encounterid,diagnosisid,dx_date,index_date
                INTO #target_primary_d0
                FROM x WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_target_primary_d0
                  ON #target_primary_d0(patid);
                """
            )

            def make_target(
                mode: str,
                phenotype: str,
                evidence_where: str,
                birth_expr: str,
            ) -> None:
                table = f"#target_{mode}_{phenotype}"
                mode_filter = "1=1" if mode == "primary" else "dx_date IS NOT NULL"
                con.exec_driver_sql(
                    f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table}"
                )
                con.exec_driver_sql(
                    f"""
                    ;WITH q AS (
                      SELECT *,
                             ROW_NUMBER() OVER (
                               PARTITION BY patid
                               ORDER BY target_index_date,
                                        encounterid,
                                        diagnosisid,
                                        condition_occurrence_id
                             ) AS rn
                      FROM #audit_target_enc
                      WHERE {evidence_where} AND {mode_filter}
                    )
                    SELECT patid,
                           encounterid,
                           diagnosisid,
                           dx_date,
                           target_index_date AS index_date
                    INTO {table}
                    FROM q
                    WHERE rn=1
                      AND FLOOR(
                            DATEDIFF(day,{birth_expr},target_index_date)/365.0
                          )>=18;
                    CREATE UNIQUE CLUSTERED INDEX IX_target_{mode}_{phenotype}
                      ON {table}(patid);
                    """
                )

            # Harmonized D0 is independently reranked after the recorded-date filter,
            # matching the post-freeze sensitivity. Primary D0 remains anchored to the
            # already selected source-faithful episode above.
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
                    con,
                    f"#src_primary_{key}",
                    f"#src_harmonized_{key}",
                )
                target_results[phenotype] = _transition(
                    con,
                    f"#target_primary_{key}",
                    f"#target_harmonized_{key}",
                )

                if source_results[phenotype]["primary_patients"] != EXPECTED_SOURCE_PRIMARY[phenotype]:
                    raise RuntimeError(
                        f"{phenotype} primary source anchor mismatch: "
                        f"{source_results[phenotype]}"
                    )
                if source_results[phenotype]["harmonized_patients"] != EXPECTED_SOURCE_HARMONIZED[phenotype]:
                    raise RuntimeError(
                        f"{phenotype} harmonized source anchor mismatch: "
                        f"{source_results[phenotype]}"
                    )
                if target_results[phenotype]["primary_patients"] != EXPECTED_TARGET_PRIMARY[phenotype]:
                    raise RuntimeError(
                        f"{phenotype} primary OMOP anchor mismatch: "
                        f"{target_results[phenotype]}"
                    )
                if target_results[phenotype]["harmonized_patients"] != EXPECTED_TARGET_HARMONIZED[phenotype]:
                    raise RuntimeError(
                        f"{phenotype} harmonized OMOP anchor mismatch: "
                        f"{target_results[phenotype]}"
                    )

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
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    output_path = out_dir / "stage_c_harmonization_transition_audit.json"
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

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
        description="Audit Stage C patient and episode transitions after diagnosis-date harmonization"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
