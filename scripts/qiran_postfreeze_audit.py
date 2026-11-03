"""Read-only, aggregate-only, post-freeze audits of source-vs-OMOP D0 attrition.

Execute in the institutional pcornet-omop-validation repository; the module
requires the existing ETL config, SQL Server access, and its locked code lists.
This script makes NO changes to permanent database tables or study definitions.

Results are restricted aggregates, stored under an ignored local results path.
Do NOT publish outputs without institutional disclosure review.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists
from pcornet_omop_validation.study.stroke_codes import (
    ICD9_STROKE_CODES, ICD10_STROKE_CODES,
)

FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
D0_SOURCE_N = 9815
D0_OMOP_N = 6001
D0_SOURCE_ONLY_N = 3814
FROZEN_OMOP_PERSON_N = 27089
SMALL_CELL = 11


def schema_name(raw: object) -> str:
    val = str(raw or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", val) is None:
        raise ValueError(f"Unsafe schema identifier: {val!r}")
    return val


def norm(c: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255),{c})))),'.','')"


def short(c: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50),{c}))))"


def literals(codes: set[str] | frozenset[str]) -> str:
    if not codes or not all(re.fullmatch(r"[A-Z0-9]+", v) for v in codes):
        raise ValueError("Unexpected locked code-list format")
    return ",".join("'" + c + "'" for c in sorted(codes))


def scalar(con: Any, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one())


def rows(con: Any, sql: str) -> list[dict[str, Any]]:
    return [dict(x) for x in con.execute(text(sql)).mappings().all()]


def finite_number(n: Any) -> float | int | None:
    if n is None:
        return None
    if hasattr(n, "item"):
        n = n.item()
    if isinstance(n, int):
        return n
    v = float(n)
    return v if math.isfinite(v) else None


def clean(rowset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{str(k): finite_number(v) if not isinstance(v, (str, type(None))) else v
             for k, v in record.items()} for record in rowset]


def suppress_partition(rows_in: list[dict[str, Any]], partition_key: str, n_key: str = "n") -> list[dict[str, Any]]:
    """Mask n < 11 and a complementary n when exactly one small count occurs.

    This is a screening safeguard, NOT institutional disclosure authorization.
    Other fields from suppressed rows are also masked.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows_in:
        groups.setdefault(str(r.get(partition_key)), []).append(dict(r))
    result = []
    for values in groups.values():
        masked = {i for i, v in enumerate(values) if int(v.get(n_key) or 0) < SMALL_CELL}
        if len(masked) == 1 and len(values) > 1:
            options = sorted((int(v[n_key]), i) for i, v in enumerate(values) if i not in masked)
            masked.add(options[0][1])
        for i, v in enumerate(values):
            if i in masked:
                for field in v:
                    if field not in (partition_key, "cohort", "era", "year_band", "enc_type", "statistic", "index_year", "admission_year"):
                        v[field] = None
                v["suppressed"] = True
            else:
                v["suppressed"] = False
            result.append(v)
    return result



def screen_descriptives(descriptives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conservatively mask sex breakdown if any small nonzero sex cell occurs.

    The analysis still retains means/SDs for large D0 groups.
    """
    out=[]
    for raw in descriptives:
        r=dict(raw)
        counts=[int(r.get(k) or 0) for k in ('female_n','male_n','other_sex_n')]
        if any(0 < v < SMALL_CELL for v in counts):
            for k in ('female_n','male_n','other_sex_n'):
                r[k]=None
            r['sex_counts_screened']=True
        else:
            r['sex_counts_screened']=False
        n=int(r['n'])
        m=int(r.get('selected_missing_dx_date_n') or 0)
        if (0 < m < SMALL_CELL) or (0 < n-m < SMALL_CELL):
            r['selected_missing_dx_date_n']=None
            r['selected_missing_screened']=True
        else:
            r['selected_missing_screened']=False
        out.append(r)
    return out


def screen_small_aggregate_counts(record: dict[str, Any]) -> dict[str, Any]:
    """Screen rare findings in a result whose columns are all raw counts."""
    out=dict(record)
    if any(isinstance(v,int) and 0 < v < SMALL_CELL for v in out.values()):
        return {k: None for k in out} | {'small_count_suppressed':True}
    return out | {'small_count_suppressed':False}

def sql_setup(source: str, target: str) -> str:
    all_codes = literals(ICD9_STROKE_CODES | ICD10_STROKE_CODES)
    nine = literals(ICD9_STROKE_CODES)
    ten = literals(ICD10_STROKE_CODES)
    return f"""
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#d0_candidates') IS NOT NULL DROP TABLE #d0_candidates;
    ;WITH dx_rank AS (
      SELECT CONVERT(nvarchar(255),d.PATID) patid,
             CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
             CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,
             CAST(d.DX_DATE AS date) dx_date,
             {norm('d.DX')} norm_code,
             {short('d.DX_TYPE')} dx_type,
             ROW_NUMBER() OVER (PARTITION BY CONVERT(nvarchar(255),d.PATID),
                                         CONVERT(nvarchar(255),d.ENCOUNTERID)
                ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                CAST(d.DX_DATE AS date),{norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)) rn
      FROM [{source}].[PCORnet_DIAGNOSIS] d
      WHERE {norm('d.DX')} IN ({all_codes}) AND {short('d.PDX')}='P'
    )
    SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,x.norm_code,x.dx_type,
           CAST(e.ADMIT_DATE AS date) admit_date,
           CAST(e.DISCHARGE_DATE AS date) discharge_date,
           {short('e.ENC_TYPE')} enc_type,
           COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) index_date,
           CAST(dm.BIRTH_DATE AS date) birth_date,
           {short('dm.SEX')} sex,
           CAST(CASE WHEN x.norm_code IN ({nine}) THEN 'ICD-9-CM'
                     WHEN x.norm_code IN ({ten}) THEN 'ICD-10-CM'
                     ELSE 'unknown' END AS varchar(12)) coding_era
    INTO #d0_candidates FROM dx_rank x
    JOIN [{source}].[PCORnet_ENCOUNTER] e
       ON CONVERT(nvarchar(255),e.PATID)=x.patid
      AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
    JOIN [{source}].[PCORnet_DEMOGRAPHIC] dm
       ON CONVERT(nvarchar(255),dm.PATID)=x.patid
    WHERE x.rn=1 AND {short('e.ENC_TYPE')} IN ('EI','IP')
      AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
      AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1;
    CREATE INDEX IX_qiran_d0cand ON #d0_candidates(patid,index_date,encounterid);

    IF OBJECT_ID('tempdb..#src_d0') IS NOT NULL DROP TABLE #src_d0;
    ;WITH chosen AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY patid ORDER BY index_date,encounterid) rn
      FROM #d0_candidates WHERE index_date IS NOT NULL
    )
    SELECT * INTO #src_d0 FROM chosen
      WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
    CREATE UNIQUE CLUSTERED INDEX IX_qiran_src ON #src_d0(patid);

    IF OBJECT_ID('tempdb..#omop_d0') IS NOT NULL DROP TABLE #omop_d0;
    ;WITH chosen AS (
      SELECT d.patid,p.person_id,v.visit_occurrence_id,
             CAST(v.visit_start_date AS date) admit_date,
             CAST(v.visit_end_date AS date) discharge_date,
             CAST(p.birth_datetime AS date) birth_date,
             p.gender_concept_id,
             COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),
                      CAST(v.visit_end_date AS date)) index_date,
             ROW_NUMBER() OVER (PARTITION BY d.patid ORDER BY
               COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),
                        CAST(v.visit_end_date AS date)),v.visit_occurrence_id) rn
      FROM #d0_candidates d
      JOIN [{target}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
      JOIN [{target}].[etl_visit_occurrence_xwalk] vx
        ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
      JOIN [{target}].[visit_occurrence] v
        ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
      JOIN [{target}].[etl_condition_occurrence_xwalk] cx
        ON cx.source_domain='DIAGNOSIS'
        AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
      JOIN [{target}].[condition_occurrence] co
        ON co.condition_occurrence_id=cx.condition_occurrence_id
        AND co.person_id=p.person_id AND co.visit_occurrence_id=v.visit_occurrence_id
    )
    SELECT * INTO #omop_d0 FROM chosen
      WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
    CREATE UNIQUE CLUSTERED INDEX IX_qiran_omop ON #omop_d0(patid);

    IF OBJECT_ID('tempdb..#qiran_src') IS NOT NULL DROP TABLE #qiran_src;
    SELECT s.patid,s.encounterid,s.index_date,s.admit_date,s.discharge_date,
           s.dx_date,s.norm_code,s.dx_type,s.enc_type,s.sex,s.birth_date,s.coding_era,
           CAST(CASE WHEN o.patid IS NULL THEN 'source_only' ELSE 'shared' END AS varchar(12)) cohort,
           CAST(FLOOR(DATEDIFF(day,s.birth_date,s.index_date)/365.0) AS float) age_years,
           CAST(DATEDIFF(day,s.admit_date,s.discharge_date) AS float) los_days,
           (SELECT COUNT_BIG(*) FROM [{source}].[PCORnet_ENCOUNTER] e
             WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND e.ADMIT_DATE IS NOT NULL
               AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),s.index_date) BETWEEN 1 AND 365)
             AS prior_all,
           (SELECT COUNT_BIG(*) FROM [{source}].[PCORnet_ENCOUNTER] e
             WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND e.ADMIT_DATE IS NOT NULL
               AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),s.index_date) BETWEEN 1 AND 365
               AND {short('e.ENC_TYPE')} IN ('ED','EI','IP')) AS prior_acute
    INTO #qiran_src
    FROM #src_d0 s LEFT JOIN #omop_d0 o ON o.patid=s.patid;
    CREATE INDEX IX_qiran_group ON #qiran_src(cohort,coding_era,index_date);
    """


def sql_coverage(source: str, target: str) -> str:
    return f"""
    SELECT
      (SELECT COUNT_BIG(*) FROM [{source}].[PCORnet_DEMOGRAPHIC]) source_demographic_rows,
      (SELECT COUNT_BIG(DISTINCT CONVERT(nvarchar(255),PATID)) FROM [{source}].[PCORnet_DEMOGRAPHIC]
        WHERE PATID IS NOT NULL) source_distinct_patid,
      (SELECT COUNT_BIG(*) FROM [{source}].[PCORnet_DEMOGRAPHIC] WHERE PATID IS NULL) source_missing_patid_rows,
      (SELECT COUNT_BIG(*) FROM [{source}].[PCORnet_DEMOGRAPHIC] WHERE BIRTH_DATE IS NULL) source_missing_birth_date_rows,
      (SELECT COUNT_BIG(*) FROM [{target}].[person]) omop_person_rows,
      (SELECT COUNT_BIG(*) FROM [{target}].[person] p
        WHERE EXISTS (SELECT 1 FROM [{source}].[PCORnet_DEMOGRAPHIC] dm
           WHERE CONVERT(nvarchar(255),dm.PATID)=p.person_source_value)) omop_person_with_source,
      (SELECT COUNT_BIG(*) FROM [{target}].[person] p
        WHERE NOT EXISTS (SELECT 1 FROM [{source}].[PCORnet_DEMOGRAPHIC] dm
           WHERE CONVERT(nvarchar(255),dm.PATID)=p.person_source_value)) omop_person_without_source
    """


def sql_descriptive() -> str:
    return """
    SELECT cohort, COUNT_BIG(*) n,
      SUM(CASE WHEN sex='F' THEN CAST(1 AS bigint) ELSE 0 END) female_n,
      SUM(CASE WHEN sex='M' THEN CAST(1 AS bigint) ELSE 0 END) male_n,
      SUM(CASE WHEN sex NOT IN ('F','M') OR sex IS NULL THEN CAST(1 AS bigint) ELSE 0 END) other_sex_n,
      AVG(age_years) age_mean, STDEV(age_years) age_sd,
      AVG(los_days) los_mean, STDEV(los_days) los_sd,
      AVG(CAST(prior_all AS float)) prior_all_mean,STDEV(CAST(prior_all AS float)) prior_all_sd,
      AVG(CAST(prior_acute AS float)) prior_acute_mean,STDEV(CAST(prior_acute AS float)) prior_acute_sd,
      SUM(CASE WHEN dx_date IS NULL THEN CAST(1 AS bigint) ELSE 0 END) selected_missing_dx_date_n
    FROM #qiran_src GROUP BY cohort
    """


def year_bin_expr() -> str:
    return "CASE WHEN YEAR(index_date)<2005 THEN '<2005' " \
           "WHEN YEAR(index_date)<2010 THEN '2005-09' " \
           "WHEN YEAR(index_date)<2015 THEN '2010-14' " \
           "WHEN YEAR(index_date)<2020 THEN '2015-19' " \
           "WHEN YEAR(index_date)<2025 THEN '2020-24' ELSE '2025+' END"


def sql_strata() -> dict[str, str]:
    y = year_bin_expr()
    return {
        "index_calendar_year": """
          SELECT YEAR(index_date) index_year, cohort, COUNT_BIG(*) n,
          SUM(CASE WHEN dx_date IS NULL THEN CAST(1 AS bigint) ELSE 0 END) missing_dx_n
          FROM #qiran_src GROUP BY YEAR(index_date),cohort
        """,
        "diagnosis_coding_era": """
          SELECT coding_era era, cohort, COUNT_BIG(*) n,
          SUM(CASE WHEN dx_date IS NULL THEN CAST(1 AS bigint) ELSE 0 END) missing_dx_n
          FROM #qiran_src GROUP BY coding_era,cohort
        """,
        "index_year_bands": f"""
          SELECT {y} year_band, cohort, COUNT_BIG(*) n,
          SUM(CASE WHEN dx_date IS NULL THEN CAST(1 AS bigint) ELSE 0 END) missing_dx_n
          FROM #qiran_src GROUP BY {y},cohort
        """,
        "index_year_by_era": f"""
          SELECT {y} year_band, coding_era era,cohort,COUNT_BIG(*) n,
          SUM(CASE WHEN dx_date IS NULL THEN CAST(1 AS bigint) ELSE 0 END) missing_dx_n
          FROM #qiran_src GROUP BY {y},coding_era,cohort
        """,
        "encounter_types": """
          SELECT enc_type,cohort, COUNT_BIG(*) n
          FROM #qiran_src GROUP BY enc_type,cohort
        """,
    }



def sql_candidate_missingness(source: str) -> str:
    """Missing-DX_DATE rate in qualifying primary-stroke DIAGNOSIS rows.

    Denominator: source primary-stroke records with qualifying EI/IP overnight
    encounter, BEFORE first-event/first-patient selection. This is deliberately
    different from the final D0 source-only/shared patient counts.
    """
    nine = literals(ICD9_STROKE_CODES)
    ten = literals(ICD10_STROKE_CODES)
    all_codes = literals(ICD9_STROKE_CODES | ICD10_STROKE_CODES)
    era = (f"CASE WHEN {norm('d.DX')} IN ({nine}) THEN 'ICD-9-CM' "
           f"WHEN {norm('d.DX')} IN ({ten}) THEN 'ICD-10-CM' ELSE 'unknown' END")
    year = "YEAR(CAST(e.ADMIT_DATE AS date))"
    return f"""
    SELECT {year} admission_year, {era} era, COUNT_BIG(*) n,
       SUM(CASE WHEN d.DX_DATE IS NULL THEN CAST(1 AS bigint) ELSE 0 END) missing_dx_n
    FROM [{source}].[PCORnet_DIAGNOSIS] d
    JOIN [{source}].[PCORnet_ENCOUNTER] e
      ON CONVERT(nvarchar(255),e.PATID)=CONVERT(nvarchar(255),d.PATID)
      AND CONVERT(nvarchar(255),e.ENCOUNTERID)=CONVERT(nvarchar(255),d.ENCOUNTERID)
    WHERE {norm('d.DX')} IN ({all_codes}) AND {short('d.PDX')}='P'
       AND {short('e.ENC_TYPE')} IN ('EI','IP')
       AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
       AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1
    GROUP BY {year},{era}
    """


def smd(a: float, b: float, sd_a: float, sd_b: float) -> float | None:
    denom = math.sqrt((sd_a * sd_a + sd_b * sd_b)/2)
    return None if denom == 0 else (a-b)/denom


def compare_descriptives(rows_in: list[dict[str, Any]]) -> dict[str, float | None]:
    groups = {r['cohort']: r for r in rows_in}
    if set(groups) != {'source_only','shared'}:
        raise ValueError('Expected source_only and shared D0 comparison groups')
    a,b=groups['source_only'],groups['shared']
    output={}
    for key in ['age','los','prior_all','prior_acute']:
        output[key+'_smd_source_only_minus_shared']=smd(
            float(a[key+'_mean']),float(b[key+'_mean']),
            float(a[key+'_sd']),float(b[key+'_sd']))
    if a.get('female_n') is not None and b.get('female_n') is not None:
        pa=float(a['female_n'])/a['n'];pb=float(b['female_n'])/b['n']
        den=math.sqrt((pa*(1-pa)+pb*(1-pb))/2)
        output['female_smd_source_only_minus_shared']=(pa-pb)/den if den else None
    else:output['female_smd_source_only_minus_shared']=None
    return output


def screen_missing_stratum(row: dict[str, Any]) -> dict[str, Any]:
    r=dict(row);n=int(r['n']);m=int(r['missing_dx_n'])
    if (0 < m < SMALL_CELL) or (0 < n-m < SMALL_CELL):
        r['missing_dx_n']=None
        r['missing_dx_proportion']=None
        r['missing_count_screened']=True
    else:
        r['missing_dx_proportion']=(m/n) if n else None
        r['missing_count_screened']=False
    return r

def sql_cross_stream_overlap(source: str, target: str) -> str:
    """Conservative candidate exact source-event overlap; not an ETL defect test.

    Exclude missing-date DIAGNOSIS rows, which the frozen ETL itself excludes.
    Compare equal PATID, ENCOUNTERID, normalized source code, date, ICD-type.
    """
    dtyp = short('d.DX_TYPE')
    ctyp = short('c.CONDITION_TYPE')
    def canon_type(expr: str) -> str:
        return (f"CASE WHEN {expr} IN ('09','9','ICD9','ICD9CM') THEN 'ICD9CM' "
                f"WHEN {expr} IN ('10','ICD10','ICD10CM') THEN 'ICD10CM' ELSE {expr} END")
    return f"""
    ;WITH dg AS (
       SELECT CONVERT(nvarchar(255),d.PATID) patid,
              CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
              {norm('d.DX')} source_code,
              {canon_type(dtyp)} code_system,
              CAST(d.DX_DATE AS date) event_date,
              COUNT_BIG(*) n
       FROM [{source}].[PCORnet_DIAGNOSIS] d
       JOIN [{target}].[person] p ON CONVERT(nvarchar(255),d.PATID)=p.person_source_value
       WHERE d.DIAGNOSISID IS NOT NULL
         AND LTRIM(RTRIM(CONVERT(nvarchar(max),d.DIAGNOSISID)))<>''
         AND d.DX_DATE IS NOT NULL AND d.PATID IS NOT NULL
       GROUP BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID),
                {norm('d.DX')},{canon_type(dtyp)},CAST(d.DX_DATE AS date)
    ), cg AS (
       SELECT CONVERT(nvarchar(255),c.PATID) patid,
              CONVERT(nvarchar(255),c.ENCOUNTERID) encounterid,
              {norm('c.CONDITION')} source_code,
              {canon_type(ctyp)} code_system,
              CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date) event_date,
              COUNT_BIG(*) n
       FROM [{source}].[PCORnet_CONDITION] c
       JOIN [{target}].[person] p ON CONVERT(nvarchar(255),c.PATID)=p.person_source_value
       WHERE c.CONDITIONID IS NOT NULL
         AND LTRIM(RTRIM(CONVERT(nvarchar(max),c.CONDITIONID)))<>''
         AND c.PATID IS NOT NULL
         AND COALESCE(c.ONSET_DATE,c.REPORT_DATE) IS NOT NULL
         AND (c.RESOLVE_DATE IS NULL OR CAST(c.RESOLVE_DATE AS date) >=
              CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date))
       GROUP BY CONVERT(nvarchar(255),c.PATID),CONVERT(nvarchar(255),c.ENCOUNTERID),
                {norm('c.CONDITION')},{canon_type(ctyp)},
                CAST(COALESCE(c.ONSET_DATE,c.REPORT_DATE) AS date)
    )
    SELECT COUNT_BIG(*) matched_source_event_keys,
           COALESCE(SUM(CASE WHEN dg.n<cg.n THEN dg.n ELSE cg.n END),0) min_matched_source_records,
           COALESCE(SUM(dg.n),0) matched_diagnosis_records,
           COALESCE(SUM(cg.n),0) matched_condition_records
    FROM dg JOIN cg ON dg.patid=cg.patid AND dg.encounterid=cg.encounterid
        AND dg.source_code=cg.source_code AND dg.code_system=cg.code_system
        AND dg.event_date=cg.event_date
    """


def sql_anchors() -> str:
    return """
    SELECT (SELECT COUNT_BIG(*) FROM #src_d0) source_d0,
           (SELECT COUNT_BIG(*) FROM #omop_d0) omop_d0,
           (SELECT COUNT_BIG(*) FROM #src_d0 s
             JOIN #omop_d0 o ON s.patid=o.patid) shared_d0,
           (SELECT COUNT_BIG(*) FROM #src_d0 s
             LEFT JOIN #omop_d0 o ON s.patid=o.patid WHERE o.patid IS NULL) source_only_d0,
           (SELECT COUNT_BIG(*) FROM #omop_d0 o
             LEFT JOIN #src_d0 s ON o.patid=s.patid WHERE s.patid IS NULL) omop_only_d0
    """


# Publication anchors describe a specific institutional snapshot, not all data
# that may exist under the same table names. Diagnostic mode never reconstructs
# a patient cohort or writes a result file.
PUBLICATION_TABLE_COUNTS = {
    "source_diagnosis_rows": 11_484_577,
    "omop_person_rows": FROZEN_OMOP_PERSON_N,
    "omop_visit_rows": 1_510_957,
    "omop_condition_rows": 7_315_572,
}


def snapshot_counts(con: Any, source: str, target: str) -> dict[str, int]:
    """Read-only snapshot fingerprint using total table counts only."""
    tables = {
        "source_demographic_rows": (source, "PCORnet_DEMOGRAPHIC"),
        "source_diagnosis_rows": (source, "PCORnet_DIAGNOSIS"),
        "source_encounter_rows": (source, "PCORnet_ENCOUNTER"),
        "omop_person_rows": (target, "person"),
        "omop_visit_rows": (target, "visit_occurrence"),
        "omop_condition_rows": (target, "condition_occurrence"),
    }
    counts: dict[str, int] = {}
    for key, (schema, table) in tables.items():
        if not table_exists(con, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] not found")
        counts[key] = scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
    return counts


def snapshot_mismatches(counts: dict[str, int]) -> dict[str, tuple[int, int | None]]:
    return {
        name: (expected, counts.get(name))
        for name, expected in PUBLICATION_TABLE_COUNTS.items()
        if counts.get(name) != expected
    }


def diagnose_snapshot(config: str) -> bool:
    """Report local aggregate database counts. No cohort query or output file."""
    cfg = load_etl_config(config)
    source = schema_name(cfg.raw["sqlserver"].get("source_schema"))
    target = schema_name(cfg.raw["sqlserver"].get("target_schema"))
    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            database_name = str(con.execute(text("SELECT DB_NAME()")).scalar_one())
            counts = snapshot_counts(con, source, target)
    finally:
        engine.dispose()
    print("mode=snapshot_diagnostics_only (no research output)")
    print(f"database={database_name!r} source_schema={source!r} target_schema={target!r}")
    mismatches = snapshot_mismatches(counts)
    for name, observed in counts.items():
        expected = PUBLICATION_TABLE_COUNTS.get(name)
        if expected is None:
            print(f"{name}: observed={observed:,}; publication_reference=not_specified")
        else:
            state = "MATCH" if observed == expected else "MISMATCH"
            print(f"{name}: observed={observed:,}; publication_reference={expected:,}; {state}")
    if mismatches:
        print("diagnosis=snapshot_or_schema_mismatch; do not change anchors, rebuild ETL, or interpret this run")
        return False
    print("diagnosis=table_counts_match; rerun full cohort audit to verify D0 anchors")
    return True


def validate_anchors(anchor: dict[str, int], coverage: dict[str, int]) -> None:
    expected = {"source_d0": D0_SOURCE_N, "omop_d0": D0_OMOP_N,
                "shared_d0": D0_OMOP_N, "source_only_d0": D0_SOURCE_ONLY_N,
                "omop_only_d0": 0}
    mismatched = {k: (expected[k], anchor.get(k)) for k in expected if int(anchor.get(k, -1)) != expected[k]}
    if int(coverage.get("omop_person_rows", -1)) != FROZEN_OMOP_PERSON_N:
        mismatched["omop_person_rows"] = (FROZEN_OMOP_PERSON_N,coverage.get("omop_person_rows"))
    if mismatched:
        raise RuntimeError(
            "Published frozen cohort/target anchors NOT reproduced; do not interpret outputs: "
            + str(mismatched)
            + ". Run with --diagnose to check source and target snapshot table counts. "
            + "Do not change frozen anchors or rebuild the ETL to force a match."
        )


def output_path(path: str) -> Path:
    root = Path.cwd().resolve()
    result_base = (root / 'results').resolve()
    path_resolved = Path(path).resolve()
    if not path_resolved.is_relative_to(result_base):
        raise ValueError("Output must be inside local ignored results/ directory")
    return path_resolved


def execute(config: str, out: str) -> Path:
    cfg = load_etl_config(config)
    source = schema_name(cfg.raw["sqlserver"].get("source_schema"))
    target = schema_name(cfg.raw["sqlserver"].get("target_schema"))
    frozen_def = json.loads(Path('study_definitions/stage_c_stroke_d0_v1.json').read_text())
    if frozen_def.get('frozen_etl_sha') != FROZEN_SHA:
        raise RuntimeError('Stage C definition no longer matches frozen ETL')
    out_path = output_path(out)
    if out_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing audit: {out_path}")
    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            required = [(source,n) for n in ('PCORnet_DEMOGRAPHIC','PCORnet_ENCOUNTER','PCORnet_DIAGNOSIS','PCORnet_CONDITION')]
            required += [(target,n) for n in ('person','visit_occurrence','condition_occurrence','etl_visit_occurrence_xwalk','etl_condition_occurrence_xwalk')]
            for s, table in required:
                if not table_exists(con, s, table):
                    raise RuntimeError(f"Required table [{s}].[{table}] not found")
            print('phase=person_coverage',flush=True)
            coverage = clean(rows(con, sql_coverage(source,target)))[0]
            print('phase=reconstruct_locked_d0',flush=True)
            con.exec_driver_sql(sql_setup(source,target))
            anchors = clean(rows(con,sql_anchors()))[0]
            validate_anchors(anchors,coverage)
            print('phase=source_only_comparison',flush=True)
            descriptive = screen_descriptives(clean(rows(con,sql_descriptive())))
            descriptive_smd = compare_descriptives(descriptive)
            strata_sql = sql_strata()
            strata = {}
            for name, query in strata_sql.items():
                raw = clean(rows(con, query))
                part = ('year_band' if name in ('index_year_bands','index_year_by_era')
                        else 'index_year' if name=='index_calendar_year'
                        else 'era' if name=='diagnosis_coding_era' else 'enc_type')
                if name=='index_year_by_era':
                    # Stratification of both year band and era: mask within each stratum.
                    for entry in raw:entry['_partition'] = str(entry['year_band'])+'|'+str(entry['era'])
                    part = '_partition'
                raw = [screen_missing_stratum(r) if 'missing_dx_n' in r else r for r in raw]
                suppress = suppress_partition(raw,part)
                for entry in suppress:entry.pop('_partition',None)
                strata[name] = suppress
            print('phase=primary_stroke_candidate_missingness',flush=True)
            candidate_raw=clean(rows(con,sql_candidate_missingness(source)))
            candidate_raw=[screen_missing_stratum(r) for r in candidate_raw]
            for entry in candidate_raw:
                entry['_partition']=str(entry['admission_year'])
            candidate_strata=suppress_partition(candidate_raw,'_partition')
            for entry in candidate_strata:entry.pop('_partition',None)
            print('phase=condition_cross_stream_overlap',flush=True)
            overlap = screen_small_aggregate_counts(clean(rows(con,sql_cross_stream_overlap(source,target)))[0])
    finally:
        engine.dispose()
    output = {
        'status':'aggregate_audit_complete_unreviewed',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'frozen_etl_sha':FROZEN_SHA,
        'stage_c_definition':'stage_c_stroke_d0_v1.json',
        'scope':'Source-defined D0 stroke cohort, source-only versus D0 shared. This does not use 30- or 90-day outcome-eligible populations.',
        'anchor_checks':anchors,
        'person_coverage':coverage,
        'group_descriptives':descriptive,
        'source_only_vs_shared_standardized_differences':descriptive_smd,
        'primary_stroke_candidate_rows_by_admission_year_and_code_era':candidate_strata,
        'stratifications_screened_not_disclosure_approved':strata,
        'cross_stream_candidate_overlap':overlap,
        'cross_stream_inference':'Exact-key overlap is an audit candidate, not proof of an erroneous duplicate. One-to-many vocabulary routing is separate.',
        'disclosure':'DO NOT COMMIT, EMAIL, OR UPLOAD until institutional disclosure review. No row-level records are exported.',
    }
    out_path.parent.mkdir(parents=True,exist_ok=True)
    flags = os.O_WRONLY|os.O_CREAT|os.O_EXCL
    fd = os.open(str(out_path), flags, 0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        json.dump(output,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    print('output='+str(out_path),flush=True)
    return out_path


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,help='Local ETL config; never commit secrets')
    parser.add_argument('--output',default='results/postfreeze_qiran/qiran_selected_d0_audit.json')
    parser.add_argument('--diagnose',action='store_true',
                        help='Only compare aggregate database table counts with the frozen publication snapshot; do not produce a research output')
    args=parser.parse_args()
    if args.diagnose:
        diagnose_snapshot(args.config)
    else:
        execute(args.config,args.output)


if __name__=='__main__':
    main()
