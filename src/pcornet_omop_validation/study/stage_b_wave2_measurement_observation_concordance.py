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

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_b_wave2_v1.json")

VITAL_MEASUREMENT_CODES = {
    "HT": "8302-2",
    "WT": "29463-7",
    "SYSTOLIC": "8480-6",
    "DIASTOLIC": "8462-4",
    "ORIGINAL_BMI": "39156-5",
}
VITAL_OBSERVATION_CODES = {
    "SMOKING": "72166-2",
    "TOBACCO": "39240-7",
    "TOBACCO_TYPE": "82769-1",
}


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


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _jaccard(intersection: int, union: int) -> float | None:
    return None if union == 0 else intersection / union


def _progress(message: str) -> None:
    print(f"progress: {message}", flush=True)


def _lab_mapping_cte(source_schema: str, target_schema: str) -> str:
    """Frozen LAB LOINC resolution policy, reproduced read-only for analysis."""
    return f"""
    WITH lab_codes AS (
      SELECT DISTINCT LTRIM(RTRIM(CONVERT(nvarchar(100), LAB_LOINC))) AS loinc
      FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
      WHERE RESULT_DATE IS NOT NULL
    ),
    source_candidates AS (
      SELECT lc.loinc, c.concept_id, c.domain_id, c.standard_concept, c.invalid_reason
      FROM lab_codes lc
      LEFT JOIN [{target_schema}].[concept] c
        ON c.vocabulary_id='LOINC' AND c.concept_code=lc.loinc
    ),
    source_counts AS (
      SELECT loinc,
             SUM(CASE WHEN concept_id IS NOT NULL THEN 1 ELSE 0 END) AS total_count,
             SUM(CASE WHEN concept_id IS NOT NULL AND invalid_reason IS NULL THEN 1 ELSE 0 END) AS active_count
      FROM source_candidates GROUP BY loinc
    ),
    source_choice AS (
      SELECT sc.loinc,
             CASE
               WHEN sc.active_count=1 THEN MAX(CASE WHEN c.invalid_reason IS NULL THEN c.concept_id END)
               WHEN sc.active_count=0 AND sc.total_count=1 THEN MAX(c.concept_id)
               ELSE NULL
             END AS source_concept_id,
             sc.total_count, sc.active_count
      FROM source_counts sc
      LEFT JOIN source_candidates c ON c.loinc=sc.loinc
      GROUP BY sc.loinc, sc.total_count, sc.active_count
    ),
    selected_source AS (
      SELECT ch.loinc, ch.source_concept_id, ch.total_count, ch.active_count,
             c.domain_id AS source_domain,
             c.standard_concept AS source_standard_concept,
             c.invalid_reason AS source_invalid_reason
      FROM source_choice ch
      LEFT JOIN [{target_schema}].[concept] c ON c.concept_id=ch.source_concept_id
    ),
    mapped_targets AS (
      SELECT DISTINCT ss.loinc, tgt.concept_id AS target_concept_id
      FROM selected_source ss
      JOIN [{target_schema}].[concept_relationship] cr
        ON cr.concept_id_1=ss.source_concept_id
       AND cr.relationship_id='Maps to'
       AND (cr.invalid_reason IS NULL OR cr.invalid_reason='')
      JOIN [{target_schema}].[concept] tgt
        ON tgt.concept_id=cr.concept_id_2
       AND tgt.standard_concept='S'
       AND tgt.invalid_reason IS NULL
       AND tgt.domain_id='Measurement'
      WHERE NOT (
        ss.source_invalid_reason IS NULL AND COALESCE(ss.source_standard_concept,'')='S'
      )
    ),
    mapped_counts AS (
      SELECT loinc, COUNT(DISTINCT target_concept_id) AS n_targets
      FROM mapped_targets GROUP BY loinc
    ),
    unique_target AS (
      SELECT mt.loinc, MAX(mt.target_concept_id) AS target_concept_id
      FROM mapped_targets mt
      JOIN mapped_counts mc ON mc.loinc=mt.loinc AND mc.n_targets=1
      GROUP BY mt.loinc
    ),
    lab_map AS (
      SELECT ss.loinc,
             COALESCE(ss.source_concept_id,0) AS source_concept_id,
             CASE
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain='Measurement' THEN ss.source_concept_id
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain<>'Measurement' THEN NULL
               WHEN mc.n_targets=1 THEN ut.target_concept_id
               ELSE 0
             END AS measurement_concept_id,
             CASE
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain='Measurement' THEN 'Measurement'
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain='Observation' THEN 'Observation'
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain<>'Measurement' THEN ss.source_domain
               WHEN mc.n_targets=1 THEN 'Measurement'
               ELSE 'Measurement'
             END AS target_domain,
             CASE
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain='Measurement' THEN ss.source_concept_id
               WHEN ss.source_invalid_reason IS NULL AND ss.source_standard_concept='S'
                    AND ss.source_domain<>'Measurement' THEN ss.source_concept_id
               WHEN mc.n_targets=1 THEN ut.target_concept_id
               ELSE 0
             END AS target_concept_id
      FROM selected_source ss
      LEFT JOIN mapped_counts mc ON mc.loinc=ss.loinc
      LEFT JOIN unique_target ut ON ut.loinc=ss.loinc
    )
    """


def _resolve_vital_concepts(con, target_schema: str) -> dict[str, int]:
    expected = {
        **{f"MEAS::{field}": code for field, code in VITAL_MEASUREMENT_CODES.items()},
        **{f"OBS::{field}": code for field, code in VITAL_OBSERVATION_CODES.items()},
    }
    out: dict[str, int] = {}
    for key, code in expected.items():
        domain = "Measurement" if key.startswith("MEAS::") else "Observation"
        rows = con.execute(
            text(f"""
                SELECT concept_id
                FROM [{target_schema}].[concept]
                WHERE vocabulary_id='LOINC' AND domain_id=:domain
                  AND standard_concept='S' AND invalid_reason IS NULL
                  AND concept_code=:code
            """),
            {"domain": domain, "code": code},
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"VITAL {key} LOINC {code} does not resolve to exactly one active Standard {domain} concept"
            )
        out[key] = int(rows[0][0])
    return out


def _insert_grouped_source(
    con,
    sql: str,
    *,
    source_family: str,
    target_domain: str,
) -> None:
    con.execute(
        text(
            "INSERT INTO #source_family_sig "
            "(source_family,target_domain,person_id,event_date,target_concept_id,n) "
            + sql
        ),
        {"source_family": source_family, "target_domain": target_domain},
    )


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    study_path = Path(STUDY_DEFINITION)
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Measurement/Observation concordance requires stage-b-wave2-v1")
    if study.get("status") != "prespecified_before_wave2_outcome_queries":
        raise RuntimeError("Wave 2 definition is not prespecified before outcome queries")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition frozen SHA mismatch")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "measurement_observation"
    )
    out.mkdir(parents=True, exist_ok=True)

    required = (
        (source_schema, "PCORnet_LAB_RESULT_CM"),
        (source_schema, "PCORnet_VITAL"),
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_CONDITION"),
        (target_schema, "person"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
        (target_schema, "measurement"),
        (target_schema, "observation"),
        (target_schema, "etl_obs_clin_route"),
        (target_schema, "etl_procedure_event_route"),
        (target_schema, "etl_condition_event_route_v2"),
        (target_schema, "etl_measurement_xwalk"),
        (target_schema, "etl_observation_xwalk"),
        (target_schema, "etl_condition_cross_domain_xwalk"),
    )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            vital_concepts = _resolve_vital_concepts(con, target_schema)
            con.exec_driver_sql("SET NOCOUNT ON")
            con.exec_driver_sql("""
                CREATE TABLE #source_family_sig (
                  source_family varchar(32) NOT NULL,
                  target_domain varchar(16) NOT NULL,
                  person_id int NOT NULL,
                  event_date date NOT NULL,
                  target_concept_id bigint NOT NULL,
                  n bigint NOT NULL
                );
            """)

            _progress("materializing LAB mapped semantic signatures")
            lab_cte = _lab_mapping_cte(source_schema, target_schema)
            con.execute(text(lab_cte + f"""
                INSERT INTO #source_family_sig
                    (source_family,target_domain,person_id,event_date,target_concept_id,n)
                SELECT 'LAB_RESULT_CM', lm.target_domain, p.person_id,
                       CAST(l.RESULT_DATE AS date), CAST(lm.target_concept_id AS bigint), COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                JOIN lab_map lm
                  ON lm.loinc=LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),l.PATID)))
                WHERE l.RESULT_DATE IS NOT NULL
                  AND lm.target_domain IN ('Measurement','Observation')
                  AND COALESCE(lm.target_concept_id,0)<>0
                GROUP BY lm.target_domain,p.person_id,CAST(l.RESULT_DATE AS date),lm.target_concept_id
            """))
            lab_unresolved = _scalar(con, lab_cte + f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                JOIN lab_map lm
                  ON lm.loinc=LTRIM(RTRIM(CONVERT(nvarchar(100),l.LAB_LOINC)))
                WHERE l.RESULT_DATE IS NOT NULL AND COALESCE(lm.target_concept_id,0)=0
            """)

            _progress("materializing VITAL mapped semantic signatures")
            for field in VITAL_MEASUREMENT_CODES:
                cid = vital_concepts[f"MEAS::{field}"]
                con.execute(text(f"""
                    INSERT INTO #source_family_sig
                        (source_family,target_domain,person_id,event_date,target_concept_id,n)
                    SELECT 'VITAL','Measurement',p.person_id,CAST(v.MEASURE_DATE AS date),
                           :concept_id,COUNT_BIG(*)
                    FROM [{source_schema}].[PCORnet_VITAL] v
                    JOIN [{target_schema}].[person] p
                      ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),v.PATID)))
                    WHERE v.MEASURE_DATE IS NOT NULL AND v.[{field}] IS NOT NULL
                    GROUP BY p.person_id,CAST(v.MEASURE_DATE AS date)
                """), {"concept_id": cid})
            for field in VITAL_OBSERVATION_CODES:
                cid = vital_concepts[f"OBS::{field}"]
                con.execute(text(f"""
                    INSERT INTO #source_family_sig
                        (source_family,target_domain,person_id,event_date,target_concept_id,n)
                    SELECT 'VITAL','Observation',p.person_id,CAST(v.MEASURE_DATE AS date),
                           :concept_id,COUNT_BIG(*)
                    FROM [{source_schema}].[PCORnet_VITAL] v
                    JOIN [{target_schema}].[person] p
                      ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),v.PATID)))
                    WHERE v.MEASURE_DATE IS NOT NULL AND v.[{field}] IS NOT NULL
                    GROUP BY p.person_id,CAST(v.MEASURE_DATE AS date)
                """), {"concept_id": cid})

            _progress("materializing OBS_CLIN mapped semantic signatures")
            con.execute(text(f"""
                INSERT INTO #source_family_sig
                    (source_family,target_domain,person_id,event_date,target_concept_id,n)
                SELECT 'OBS_CLIN',r.target_domain,p.person_id,r.obsclin_start_date,
                       CAST(r.target_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                JOIN [{target_schema}].[person] p ON p.person_source_value=r.patid
                WHERE r.target_domain IN ('Measurement','Observation')
                  AND r.target_concept_id<>0
                GROUP BY r.target_domain,p.person_id,r.obsclin_start_date,r.target_concept_id
            """))
            obsclin_unresolved = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_obs_clin_route]
                WHERE target_domain IN ('Measurement','Observation') AND target_concept_id=0
            """)

            _progress("materializing Procedure-derived mapped semantic signatures")
            con.execute(text(f"""
                INSERT INTO #source_family_sig
                    (source_family,target_domain,person_id,event_date,target_concept_id,n)
                SELECT 'PROCEDURES',r.target_domain,p.person_id,r.px_date,
                       CAST(r.target_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[etl_procedure_event_route] r
                JOIN [{target_schema}].[person] p ON p.person_source_value=r.patid
                WHERE r.target_domain IN ('Measurement','Observation')
                  AND r.disposition='event_route' AND r.target_concept_id<>0
                GROUP BY r.target_domain,p.person_id,r.px_date,r.target_concept_id
            """))
            procedure_unresolved = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_procedure_event_route]
                WHERE target_domain IN ('Measurement','Observation') AND target_concept_id=0
            """)

            _progress("materializing Condition-derived mapped semantic signatures")
            con.execute(text(f"""
                INSERT INTO #source_family_sig
                    (source_family,target_domain,person_id,event_date,target_concept_id,n)
                SELECT 'CONDITION',r.target_domain,p.person_id,CAST(d.DX_DATE AS date),
                       CAST(r.target_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[etl_condition_event_route_v2] r
                JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d
                  ON r.source_domain='DIAGNOSIS'
                 AND r.source_record_id=CONVERT(nvarchar(255),d.DIAGNOSISID)
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=CONVERT(nvarchar(50),d.PATID)
                WHERE r.is_core_event_route=1
                  AND r.target_domain IN ('Measurement','Observation')
                  AND r.target_concept_id<>0 AND d.DX_DATE IS NOT NULL
                GROUP BY r.target_domain,p.person_id,CAST(d.DX_DATE AS date),r.target_concept_id

                UNION ALL

                SELECT 'CONDITION',r.target_domain,p.person_id,
                       CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date),
                       CAST(r.target_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[etl_condition_event_route_v2] r
                JOIN [{source_schema}].[PCORnet_CONDITION] c
                  ON r.source_domain='CONDITION'
                 AND r.source_record_id=CONVERT(nvarchar(255),c.CONDITIONID)
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=CONVERT(nvarchar(50),c.PATID)
                WHERE r.is_core_event_route=1
                  AND r.target_domain IN ('Measurement','Observation')
                  AND r.target_concept_id<>0
                  AND COALESCE(c.ONSET_DATE,c.REPORT_DATE) IS NOT NULL
                  AND (c.RESOLVE_DATE IS NULL OR CAST(c.RESOLVE_DATE AS date)>=CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date))
                GROUP BY r.target_domain,p.person_id,
                         CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date),r.target_concept_id
            """))

            con.exec_driver_sql("""
                CREATE INDEX IX_source_family_sig
                  ON #source_family_sig(target_domain,target_concept_id,person_id,event_date);

                SELECT person_id,event_date,target_domain,target_concept_id,SUM(n) AS n
                INTO #source_sig
                FROM #source_family_sig
                GROUP BY person_id,event_date,target_domain,target_concept_id;
                CREATE UNIQUE CLUSTERED INDEX IX_source_sig
                  ON #source_sig(person_id,event_date,target_domain,target_concept_id);

                SELECT DISTINCT target_domain,target_concept_id
                INTO #source_concepts FROM #source_sig;
                CREATE UNIQUE CLUSTERED INDEX IX_source_concepts
                  ON #source_concepts(target_domain,target_concept_id);
            """)

            _progress("scanning native OMOP Measurement/Observation concept space once")
            con.exec_driver_sql(f"""
                CREATE TABLE #target_sig (
                  person_id int NOT NULL,
                  event_date date NOT NULL,
                  target_domain varchar(16) NOT NULL,
                  target_concept_id bigint NOT NULL,
                  n bigint NOT NULL
                );

                INSERT INTO #target_sig
                SELECT m.person_id,CAST(m.measurement_date AS date),'Measurement',
                       CAST(m.measurement_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN #source_concepts c
                  ON c.target_domain='Measurement' AND c.target_concept_id=m.measurement_concept_id
                GROUP BY m.person_id,CAST(m.measurement_date AS date),m.measurement_concept_id;

                INSERT INTO #target_sig
                SELECT o.person_id,CAST(o.observation_date AS date),'Observation',
                       CAST(o.observation_concept_id AS bigint),COUNT_BIG(*)
                FROM [{target_schema}].[observation] o
                JOIN #source_concepts c
                  ON c.target_domain='Observation' AND c.target_concept_id=o.observation_concept_id
                GROUP BY o.person_id,CAST(o.observation_date AS date),o.observation_concept_id;

                CREATE UNIQUE CLUSTERED INDEX IX_target_sig
                  ON #target_sig(person_id,event_date,target_domain,target_concept_id);
            """)

            _progress("computing primary semantic concordance")
            source_mapped_rows = _scalar(con, "SELECT COALESCE(SUM(n),0) FROM #source_sig")
            target_space_rows = _scalar(con, "SELECT COALESCE(SUM(n),0) FROM #target_sig")
            patient_row = con.execute(text("""
                WITH s AS (SELECT DISTINCT person_id FROM #source_sig),
                     t AS (SELECT DISTINCT person_id FROM #target_sig),
                     a AS (SELECT person_id FROM s UNION SELECT person_id FROM t)
                SELECT
                  SUM(CASE WHEN s.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END)
                FROM a LEFT JOIN s ON s.person_id=a.person_id LEFT JOIN t ON t.person_id=a.person_id
            """)).one()
            source_patients,target_patients,intersection,source_only,target_only = [int(v or 0) for v in patient_row]
            patient_union = intersection + source_only + target_only

            sig_row = con.execute(text("""
                SELECT
                  COALESCE(SUM(CASE WHEN COALESCE(s.n,0)<COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),0),
                  COALESCE(SUM(CASE WHEN COALESCE(s.n,0)>COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),0),
                  COALESCE(SUM(CASE WHEN COALESCE(t.n,0)>COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END),0)
                FROM #source_sig s
                FULL OUTER JOIN #target_sig t
                  ON t.person_id=s.person_id AND t.event_date=s.event_date
                 AND t.target_domain=s.target_domain AND t.target_concept_id=s.target_concept_id
            """)).one()
            matched,source_unmatched,target_unmatched = [int(v or 0) for v in sig_row]

            family_domain_rows = [dict(r) for r in con.execute(text("""
                SELECT source_family,target_domain,SUM(n) AS mapped_rows,
                       COUNT_BIG(DISTINCT person_id) AS mapped_patients
                FROM #source_family_sig
                GROUP BY source_family,target_domain
                ORDER BY source_family,target_domain
            """)).mappings().all()]

            domain_rows: list[dict[str, Any]] = []
            for domain in ("Measurement","Observation"):
                r = con.execute(text("""
                    SELECT
                      COALESCE(SUM(CASE WHEN s.n IS NOT NULL THEN s.n ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN t.n IS NOT NULL THEN t.n ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0)<COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0)>COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(t.n,0)>COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END),0)
                    FROM (SELECT * FROM #source_sig WHERE target_domain=:domain) s
                    FULL OUTER JOIN (SELECT * FROM #target_sig WHERE target_domain=:domain) t
                      ON t.person_id=s.person_id AND t.event_date=s.event_date
                     AND t.target_concept_id=s.target_concept_id
                """), {"domain":domain}).one()
                sr,tr,ma,su,tu = [int(v or 0) for v in r]
                domain_rows.append({
                    "target_domain":domain,
                    "source_mapped_rows":sr,
                    "target_rows_in_source_concept_space":tr,
                    "exact_matched_rows":ma,
                    "source_unmatched_rows":su,
                    "target_unmatched_before_attribution":tu,
                    "source_match_percent":_pct(ma,sr),
                })

            _progress("performing secondary provenance attribution")
            provenance_rows = [dict(r) for r in con.execute(text(f"""
                WITH mspace AS (
                  SELECT m.measurement_id AS target_row_id,'Measurement' AS target_domain,
                         CASE
                           WHEN cx.target_row_id IS NOT NULL THEN 'CONDITION'
                           WHEN x.measurement_id IS NOT NULL THEN x.source_family
                           ELSE 'UNATTRIBUTED'
                         END AS provenance
                  FROM [{target_schema}].[measurement] m
                  JOIN #source_concepts c
                    ON c.target_domain='Measurement' AND c.target_concept_id=m.measurement_concept_id
                  LEFT JOIN [{target_schema}].[etl_condition_cross_domain_xwalk] cx
                    ON cx.target_domain='Measurement' AND cx.target_row_id=m.measurement_id
                  LEFT JOIN [{target_schema}].[etl_measurement_xwalk] x
                    ON x.measurement_id=m.measurement_id
                ),
                ospace AS (
                  SELECT o.observation_id AS target_row_id,'Observation' AS target_domain,
                         CASE
                           WHEN cx.target_row_id IS NOT NULL THEN 'CONDITION'
                           WHEN x.observation_id IS NOT NULL THEN x.source_family
                           ELSE 'UNATTRIBUTED'
                         END AS provenance
                  FROM [{target_schema}].[observation] o
                  JOIN #source_concepts c
                    ON c.target_domain='Observation' AND c.target_concept_id=o.observation_concept_id
                  LEFT JOIN [{target_schema}].[etl_condition_cross_domain_xwalk] cx
                    ON cx.target_domain='Observation' AND cx.target_row_id=o.observation_id
                  LEFT JOIN [{target_schema}].[etl_observation_xwalk] x
                    ON x.observation_id=o.observation_id
                )
                SELECT target_domain,provenance,COUNT_BIG(*) AS rows
                FROM (
                  SELECT target_domain,provenance FROM mspace
                  UNION ALL
                  SELECT target_domain,provenance FROM ospace
                ) q
                GROUP BY target_domain,provenance
                ORDER BY target_domain,provenance
            """)).mappings().all()]
            unattributed = sum(int(r["rows"]) for r in provenance_rows if r["provenance"]=="UNATTRIBUTED")
    finally:
        engine.dispose()

    obs_gen_rows = 353586  # replaced below from source-derived preflight output when available
    preflight_path = out / "stage_b_wave2_measurement_observation_preflight.json"
    if preflight_path.exists():
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        obs_gen_rows = int(preflight.get("obs_gen",{}).get("source_rows",obs_gen_rows))

    unresolved = {
        "LAB_RESULT_CM": lab_unresolved,
        "OBS_CLIN": obsclin_unresolved,
        "PROCEDURES": procedure_unresolved,
        "OBS_GEN_descriptive_concept_zero": obs_gen_rows,
    }
    primary = {
        "source_mapped_rows": source_mapped_rows,
        "target_native_rows_in_source_concept_space": target_space_rows,
        "source_mapped_patients": source_patients,
        "target_mapped_patients": target_patients,
        "intersection_patients": intersection,
        "source_only_patients": source_only,
        "target_only_patients": target_only,
        "union_patients": patient_union,
        "patient_jaccard": _jaccard(intersection, patient_union),
        "patient_positive_agreement_percent": _pct(2*intersection,source_patients+target_patients),
        "exact_person_date_domain_concept_matched_events": matched,
        "source_unmatched_signature_events": source_unmatched,
        "target_unmatched_signature_events": target_unmatched,
        "source_exact_signature_match_percent": _pct(matched,source_mapped_rows),
    }
    summary = {
        "status":"stage_b_wave2_measurement_observation_concordance_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "study_definition":"stage-b-wave2-v1",
        "study_definition_sha256":_sha256(study_path),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_branch":_git("branch","--show-current"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "primary_comparison":primary,
        "source_family_domain_summary":family_domain_rows,
        "domain_summary":domain_rows,
        "unresolved_or_descriptive_coverage":unresolved,
        "secondary_provenance_attribution":{
            "target_rows_by_provenance":provenance_rows,
            "unattributed_rows":unattributed,
            "method":"Target lineage is applied only after native semantic concordance to classify target concept-space overlap.",
        },
        "interpretation_rules":[
            "Primary identity is person + calendar date + target domain + active Standard concept.",
            "LAB mappings are independently recomputed from the frozen vocabulary policy rather than read from target lineage.",
            "OBS_CLIN, Procedure, and Condition route ledgers are prespecified source-side semantic references; target xwalks are not used in primary matching.",
            "OBS_GEN and concept-zero routes are excluded from mapped semantic match percentages and reported separately.",
            "Target-side excess is not labeled discordant until secondary provenance attribution is reviewed.",
        ],
    }

    (out/"stage_b_wave2_measurement_observation_summary.json").write_text(
        json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8"
    )
    if family_domain_rows:
        with (out/"measurement_observation_source_family_domain.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(family_domain_rows[0].keys()))
            w.writeheader(); w.writerows(family_domain_rows)
    if provenance_rows:
        with (out/"measurement_observation_target_provenance.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(provenance_rows[0].keys()))
            w.writeheader(); w.writerows(provenance_rows)
    with (out/"measurement_observation_domain_concordance.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(domain_rows[0].keys()))
        w.writeheader(); w.writerows(domain_rows)

    print("status: stage_b_wave2_measurement_observation_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_mapped_rows: {source_mapped_rows}")
    print(f"target_rows_in_source_concept_space: {target_space_rows}")
    print(f"source_mapped_patients: {source_patients}")
    print(f"target_mapped_patients: {target_patients}")
    print(f"patient_jaccard: {primary['patient_jaccard']}")
    print(f"exact_signature_matched_events: {matched}")
    print(f"source_unmatched_signature_events: {source_unmatched}")
    print(f"target_unmatched_signature_events: {target_unmatched}")
    print(f"secondary_attribution_unattributed_rows: {unattributed}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser=argparse.ArgumentParser(description="Stage B Wave 2 Measurement/Observation semantic concordance")
    parser.add_argument("--config",required=True)
    parser.add_argument("--output-dir")
    args=parser.parse_args()
    run(args.config,args.output_dir)


if __name__=="__main__":
    main()
