#!/usr/bin/env python3
"""Post-freeze, read-only 90-day observability and mortality diagnostic.

Runs only in an authorized institutional environment. Recreates the locked Stage
D D0 source/lineage-retained cohorts and the exact representation-specific 90-day
coverage and outcome rules. Adds source and target death timing and aggregate
age/calendar comparisons. Creates SQL Server #temp tables in tempdb only.

This is a DIAGNOSTIC, not an amendment to the prespecified Stage D estimand.
No row-level output is written. Aggregate results remain institution-controlled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
DEFAULT_PARENT = Path("results/publication_analysis/stage_d_analytical_equivalence/stage_d_stroke_analytical_equivalence.json")
DEFAULT_OUTPUT = Path("results/publication_analysis/postfreeze_observability_mortality/postfreeze_observability_mortality_90d.json")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _schema(s: Any) -> str:
    value = str(s or "dbo")
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError("Unsafe schema identifier")
    return value


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values: Any) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _load_anchors(parent: Path) -> dict[str, Any]:
    if not parent.exists():
        raise FileNotFoundError(
            f"Completed Stage D JSON was not found: {parent}. "
            "Supply --stage-d-summary PATH to the existing original run, do not rerun Stage D."
        )
    payload = json.loads(parent.read_text(encoding="utf-8"))
    if payload.get("status") != "stage_d_stroke_analytical_equivalence_complete":
        raise RuntimeError("Parent Stage D result is not marked complete")
    if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Parent Stage D frozen ETL SHA mismatch")
    if payload.get("study_definition_sha256") != _sha256(STUDY_PATH):
        raise RuntimeError("Parent Stage D study-definition SHA mismatch")
    if payload.get("inherited_d0_definition_sha256") != _sha256(D0_PATH):
        raise RuntimeError("Parent D0 definition SHA mismatch")
    return payload


def _cohort_sql(src: str, tgt: str, stroke_codes: str) -> list[str]:
    """Copied selection semantics from locked stage_d_stroke_analytical_equivalence.py."""
    return [
        f"""
        ;WITH dx_rank AS (
          SELECT CONVERT(nvarchar(255),d.PATID) patid,
                 CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
                 CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,
                 CAST(d.DX_DATE AS date) dx_date,
                 ROW_NUMBER() OVER (
                   PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                   ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                     CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)
                 ) rn
          FROM [{src}].[PCORnet_DIAGNOSIS] d
          WHERE {_norm('d.DX')} IN ({stroke_codes}) AND {_short('d.PDX')}='P'
        ), enc AS (
          SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
                 CAST(e.ADMIT_DATE AS date) admit_date,
                 CAST(e.DISCHARGE_DATE AS date) discharge_date,
                 COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) index_date,
                 CAST(dm.BIRTH_DATE AS date) birth_date
          FROM dx_rank x
          JOIN [{src}].[PCORnet_ENCOUNTER] e
            ON CONVERT(nvarchar(255),e.PATID)=x.patid
           AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
          JOIN [{src}].[PCORnet_DEMOGRAPHIC] dm
            ON CONVERT(nvarchar(255),dm.PATID)=x.patid
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
        CREATE UNIQUE CLUSTERED INDEX IX_audit90_src_d0 ON #src_d0(patid);
        """,
        f"""
        SELECT s.patid,p.person_id,s.encounterid,s.index_date source_index_date,
               CAST(v.visit_start_date AS date) omop_admit_date,
               CAST(v.visit_end_date AS date) omop_discharge_date,
               COALESCE(CAST(co.condition_start_date AS date),
                 CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) omop_index_date,
               v.visit_occurrence_id,co.condition_occurrence_id
        INTO #omop_d0_all
        FROM #src_d0 s
        JOIN [{tgt}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=s.patid
        JOIN [{tgt}].[etl_visit_occurrence_xwalk] vx
          ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
        JOIN [{tgt}].[visit_occurrence] v
          ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
        JOIN [{tgt}].[etl_condition_occurrence_xwalk] cx
          ON cx.source_domain='DIAGNOSIS'
         AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
        JOIN [{tgt}].[condition_occurrence] co
          ON co.condition_occurrence_id=cx.condition_occurrence_id
         AND co.person_id=p.person_id AND co.visit_occurrence_id=v.visit_occurrence_id;
        """,
        """
        ;WITH r AS (
          SELECT *,ROW_NUMBER() OVER(PARTITION BY patid ORDER BY omop_index_date,condition_occurrence_id) rn
          FROM #omop_d0_all
        )
        SELECT patid,person_id,encounterid,source_index_date,omop_admit_date,
               omop_discharge_date,omop_index_date,visit_occurrence_id
        INTO #omop_d0 FROM r WHERE rn=1;
        CREATE UNIQUE CLUSTERED INDEX IX_audit90_omop_d0 ON #omop_d0(patid);
        """,
    ]


def _flags_sql(src: str, tgt: str) -> str:
    # Same logical coverage rule as Stage D: a SINGLE source ENROLLMENT or OMOP
    # observation_period row spans discharge to the end of day 90.
    # The audit counts events even when covered90=0. Those are diagnostics and
    # are NOT part of the locked primary risk numerator.
    return f"""
    SELECT s.patid,
      s.admit_date,s.discharge_date,s.index_date,s.birth_date,
      CAST(FLOOR(DATEDIFF(day,s.birth_date,s.index_date)/365.0) AS int) AS age_years,
      CASE WHEN YEAR(s.admit_date) BETWEEN 2010 AND 2016 THEN '2010-2016'
           WHEN YEAR(s.admit_date) BETWEEN 2017 AND 2018 THEN '2017-2018'
           WHEN YEAR(s.admit_date) BETWEEN 2019 AND 2024 THEN '2019-2024'
           ELSE 'outside_eras' END AS admission_era,
      CASE WHEN o.patid IS NULL THEN 0 ELSE 1 END AS retained_in_omop,
      CASE WHEN o.patid IS NOT NULL AND o.omop_index_date=s.index_date THEN 1 ELSE 0 END AS same_index,
      CASE WHEN EXISTS (
          SELECT 1 FROM [{src}].[PCORnet_ENROLLMENT] en
          WHERE CONVERT(nvarchar(255),en.PATID)=s.patid
            AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
            AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,90,s.discharge_date)
        ) THEN 1 ELSE 0 END AS source_covered90,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN EXISTS (
             SELECT 1 FROM [{tgt}].[observation_period] op
             WHERE op.person_id=o.person_id
               AND op.observation_period_start_date<=o.omop_discharge_date
               AND op.observation_period_end_date>=DATEADD(day,90,o.omop_discharge_date)
           ) THEN 1 ELSE 0 END AS target_covered90,
      CASE WHEN EXISTS (
          SELECT 1 FROM [{src}].[PCORnet_ENCOUNTER] e
          WHERE CONVERT(nvarchar(255),e.PATID)=s.patid
            AND CAST(e.ADMIT_DATE AS date)>s.discharge_date
            AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,s.discharge_date)
            AND {_short('e.ENC_TYPE')} IN ('ED','EI','IP')
        ) THEN 1 ELSE 0 END AS source_event90,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN EXISTS (
             SELECT 1 FROM [{tgt}].[visit_occurrence] v
             WHERE v.person_id=o.person_id
               AND CAST(v.visit_start_date AS date)>o.omop_discharge_date
               AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,o.omop_discharge_date)
               AND v.visit_concept_id IN (9203,262,9201)
           ) THEN 1 ELSE 0 END AS target_event90,
      sd.first_death_date AS source_death_date,
      td.first_death_date AS target_death_date,
      CASE WHEN sd.first_death_date >= s.discharge_date
             AND sd.first_death_date < DATEADD(day,90,s.discharge_date)
           THEN 1 ELSE 0 END AS source_death_before90,
      CASE WHEN sd.first_death_date >= s.discharge_date
             AND sd.first_death_date <= DATEADD(day,90,s.discharge_date)
           THEN 1 ELSE 0 END AS source_death_through90,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN td.first_death_date >= o.omop_discharge_date
             AND td.first_death_date < DATEADD(day,90,o.omop_discharge_date)
           THEN 1 ELSE 0 END AS target_death_before90,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN sd.first_death_date IS NOT NULL AND td.first_death_date IS NOT NULL
            AND sd.first_death_date<>td.first_death_date THEN 1 ELSE 0 END AS death_date_disagreement,
      CASE WHEN sd.first_death_date IS NOT NULL AND sd.first_death_date<s.discharge_date
           THEN 1 ELSE 0 END AS source_death_before_discharge
    INTO #audit90
    FROM #src_d0 s
    LEFT JOIN #omop_d0 o ON o.patid=s.patid
    OUTER APPLY (
      SELECT MIN(CAST(d.DEATH_DATE AS date)) AS first_death_date
      FROM [{src}].[PCORnet_DEATH] d
      WHERE CONVERT(nvarchar(255),d.PATID)=s.patid AND d.DEATH_DATE IS NOT NULL
    ) sd
    OUTER APPLY (
      SELECT MIN(CAST(d.death_date AS date)) AS first_death_date
      FROM [{tgt}].[death] d
      WHERE d.person_id=o.person_id AND d.death_date IS NOT NULL
    ) td;
    CREATE UNIQUE CLUSTERED INDEX IX_audit90_flags ON #audit90(patid);
    """


# Aggregate expressions use COUNT_BIG and COALESCE to prevent NULL counts.
METRICS = {
    "n": "COUNT_BIG(*)",
    "mean_index_age": "CAST(ROUND(AVG(CAST(age_years AS float)),1) AS decimal(6,1))",
    "source_covered90": "SUM(CASE WHEN source_covered90=1 THEN 1 ELSE 0 END)",
    "source_not_covered90": "SUM(CASE WHEN source_covered90=0 THEN 1 ELSE 0 END)",
    "source_events90_any": "SUM(CASE WHEN source_event90=1 THEN 1 ELSE 0 END)",
    "source_events90_covered": "SUM(CASE WHEN source_event90=1 AND source_covered90=1 THEN 1 ELSE 0 END)",
    "source_events90_without_full_coverage": "SUM(CASE WHEN source_event90=1 AND source_covered90=0 THEN 1 ELSE 0 END)",
    "source_deaths_before_day90": "SUM(CASE WHEN source_death_before90=1 THEN 1 ELSE 0 END)",
    "source_deaths_through_day90": "SUM(CASE WHEN source_death_through90=1 THEN 1 ELSE 0 END)",
    "source_deaths_before90_covered": "SUM(CASE WHEN source_death_before90=1 AND source_covered90=1 THEN 1 ELSE 0 END)",
    "source_deaths_before90_not_covered": "SUM(CASE WHEN source_death_before90=1 AND source_covered90=0 THEN 1 ELSE 0 END)",
    "source_deaths_before_discharge": "SUM(CASE WHEN source_death_before_discharge=1 THEN 1 ELSE 0 END)",
    "target_covered90": "SUM(CASE WHEN target_covered90=1 THEN 1 ELSE 0 END)",
    "target_not_covered90": "SUM(CASE WHEN target_covered90=0 THEN 1 ELSE 0 END)",
    "target_events90_any": "SUM(CASE WHEN target_event90=1 THEN 1 ELSE 0 END)",
    "target_events90_covered": "SUM(CASE WHEN target_event90=1 AND target_covered90=1 THEN 1 ELSE 0 END)",
    "target_events90_without_full_coverage": "SUM(CASE WHEN target_event90=1 AND target_covered90=0 THEN 1 ELSE 0 END)",
    "target_deaths_before_day90": "SUM(CASE WHEN target_death_before90=1 THEN 1 ELSE 0 END)",
    "target_deaths_before90_covered": "SUM(CASE WHEN target_death_before90=1 AND target_covered90=1 THEN 1 ELSE 0 END)",
    "target_deaths_before90_not_covered": "SUM(CASE WHEN target_death_before90=1 AND target_covered90=0 THEN 1 ELSE 0 END)",
    "death_date_disagreement": "SUM(CASE WHEN death_date_disagreement=1 THEN 1 ELSE 0 END)",
}


def _select_aggregates(where: str, label: str, group_by: str = "") -> str:
    select = ",\n      ".join(f"{expr} AS {name}" for name, expr in METRICS.items())
    if group_by:
        return f"SELECT '{label}' AS group_type, {group_by} AS group_label, {select} FROM #audit90 WHERE {where} GROUP BY {group_by} ORDER BY {group_by}"
    return f"SELECT '{label}' AS group_type, '{label}' AS group_label, {select} FROM #audit90 WHERE {where}"


def _numeric_result(row: Any) -> dict[str, Any]:
    return {key: (float(val) if key == "mean_index_age" and val is not None
                  else int(val) if isinstance(val, (int,)) or (hasattr(val, "__int__") and val is not None)
                  else val) for key, val in dict(row).items()}


def _query_groups(con: Any) -> list[dict[str, Any]]:
    groups = [
        ("source_d0_all", "1=1", ""),
        ("omop_lineage_retained", "retained_in_omop=1", ""),
        ("source_only", "retained_in_omop=0", ""),
        ("shared_exact_index", "same_index=1", ""),
        ("source_coverage_status", "1=1", "CASE WHEN source_covered90=1 THEN 'covered' ELSE 'not_covered' END"),
        ("target_coverage_status", "retained_in_omop=1", "CASE WHEN target_covered90=1 THEN 'covered' ELSE 'not_covered' END"),
        ("source_admission_era", "1=1", "admission_era"),
        ("source_admission_era_by_coverage", "1=1", "CONCAT(admission_era,' / ',CASE WHEN source_covered90=1 THEN 'covered' ELSE 'not_covered' END)"),
        ("target_admission_era_by_coverage", "retained_in_omop=1", "CONCAT(admission_era,' / ',CASE WHEN target_covered90=1 THEN 'covered' ELSE 'not_covered' END)"),
        ("shared_coverage_pair", "retained_in_omop=1", "CONCAT('source=',source_covered90,',target=',target_covered90)"),
    ]
    out = []
    for label, where, group in groups:
        for row in con.exec_driver_sql(_select_aggregates(where, label, group)).mappings().all():
            out.append(_numeric_result(row))
    return out


def _validate_matches(rows: list[dict[str, Any]], original: dict[str, Any]) -> None:
    lookup = {(r["group_type"], r["group_label"]): r for r in rows}
    src = lookup[("source_d0_all", "source_d0_all")]
    tgt = lookup[("omop_lineage_retained", "omop_lineage_retained")]
    exact = lookup[("shared_exact_index", "shared_exact_index")]
    old = original["results"]["D0_reproduction"]
    expected = (int(old["source_patients"]),
                int(old["lineage_faithful_omop_patients"]),
                int(old["shared_exact_index_patients"]))
    observed = (src["n"], tgt["n"], exact["n"])
    if observed != expected:
        raise RuntimeError(f"Cohort lock FAILED: source/OMOP/exact {observed} != {expected}")
    primary = original["results"]["primary_fixed_index_90d"]["label_counts"]
    fixed_count = None
    # Exactly reproducing Stage D fixed-index, both-sided coverage eligibility.
    # Source and target coverage-pair aggregate includes all source-linked OMOP D0
    # patients. All original D0 shared patients had exact index, verified above.
    for r in rows:
        if r["group_type"] == "shared_coverage_pair" and r["group_label"] == "source=1,target=1":
            fixed_count = int(r["n"])
    if fixed_count != int(primary["eligible"]):
        raise RuntimeError(f"Fixed-90 cohort lock FAILED: {fixed_count} != {primary['eligible']}")
    e2e = original["results"]["secondary_end_to_end_90d"]
    checks = [
        ("source 90d denominator", int(src["source_covered90"]), int(e2e["source_eligible"])),
        ("OMOP 90d denominator", int(tgt["target_covered90"]), int(e2e["omop_eligible"])),
        ("source 90d numerator", int(src["source_events90_covered"]), int(e2e["source_events"])),
        ("OMOP 90d numerator", int(tgt["target_events90_covered"]), int(e2e["omop_events"])),
    ]
    for label, actual, locked in checks:
        if actual != locked:
            raise RuntimeError(f"{label} mismatch: {actual} vs locked {locked}")


def run(config_path: str, stage_d_summary: Path, out: Path) -> dict[str, Any]:
    # Only import institutional dependencies when executing against the DB.
    from pcornet_omop_validation.etl.config import load_etl_config
    from pcornet_omop_validation.etl.database import make_engine
    from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

    if out.resolve() == stage_d_summary.resolve():
        raise ValueError("Audit output cannot overwrite the locked Stage D summary")
    orig = _load_anchors(stage_d_summary)
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Study definition not anchored to expected frozen ETL")
    src = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    tgt = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(cfg)
    rows: list[dict[str, Any]] = []
    try:
        # No permanent changes. SQL Server temporary tables are connection-scoped.
        with engine.connect() as con:
            con.exec_driver_sql("SET LOCK_TIMEOUT 30000")
            print("Reproducing locked Stage D source D0 and lineage-traced OMOP D0...", flush=True)
            for sql in _cohort_sql(src, tgt, _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))):
                con.exec_driver_sql(sql)
            print("Computing original coverage/outcome flags and death timing...", flush=True)
            con.exec_driver_sql(_flags_sql(src, tgt))
            print("Computing aggregate summaries...", flush=True)
            rows = _query_groups(con)
            _validate_matches(rows, orig)
    finally:
        engine.dispose()

    payload = {
        "status": "postfreeze_observability_mortality_90d_diagnostic_complete",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_repo_sha": _git("rev-parse", "HEAD"),
        "analysis_repo_clean": _git("status", "--porcelain") == "",
        "parent_stage_d_sha256": _sha256(stage_d_summary),
        "study_definition_sha256": _sha256(STUDY_PATH),
        "d0_definition_sha256": _sha256(D0_PATH),
        "cohort_and_outcome_anchors_match": True,
        "definitions": {
            "source_cohort": "Locked Stage D D0 source first qualifying index episode, post-index adult check",
            "omop_cohort": "Exact D0 episode trace via visit and condition xwalks (not native OMOP phenotype)",
            "source_covered90": "One PCORnet_ENROLLMENT row spans discharge date through discharge+90 days",
            "target_covered90": "One OMOP observation_period row spans discharge date through discharge+90 days",
            "events90": "Any ED/EI/IP start strictly after discharge and by day 90, including among uncovered patients for diagnostic purposes only",
            "death_before90": "Recorded death date >= discharge and < discharge+90 days (does not imply death caused loss of coverage)",
            "death_through90": "Recorded death date >= discharge and <= discharge+90 days",
            "age": "FLOOR(DATEDIFF(day,birth_date,index_date)/365.0), consistent with locked adult rule",
            "admission_era": "2010-2016, 2017-2018, 2019-2024, outside_eras",
        },
        "aggregate_rows": rows,
        "warnings": [
            "Diagnostic-only: do not replace the frozen Stage D estimands.",
            "Death ascertainment and enrollment/observation coverage are observational data properties. Association does not prove death CAUSED coverage loss.",
            "No patient-level rows are exported. Aggregate disclosure approval is still required before sharing outside the institutional environment.",
            "Aggregate subgroup cells may be small and may be inferable by subtraction; the JSON is NOT automatically disclosure-safe.",
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print("status: postfreeze_observability_mortality_90d_diagnostic_complete")
    print("anchors: cohort, fixed-index eligibility, and 90-day end-to-end outcome counts verified")
    print(f"private_aggregate_output: {out}")
    print("DO NOT upload the JSON or paste individual small cells without institutional disclosure approval")
    return payload


def self_test() -> None:
    sql = "\n".join(_cohort_sql("dbo", "omop", "'I63'")) + _flags_sql("dbo", "omop")
    assert "INTO #src_d0" in sql and "INTO #omop_d0" in sql and "INTO #audit90" in sql
    assert "DATEADD(day,90" in sql and "PCORnet_DEATH" in sql and "observation_period" in sql
    assert "DROP TABLE [" not in sql.upper() and "DELETE FROM [" not in sql.upper()
    assert "INSERT INTO [" not in sql.upper() and "UPDATE [" not in sql.upper()
    assert _schema("omop_54") == "omop_54"
    try:
        _schema("bad;DROP TABLE foo")
        raise AssertionError("Unsafe schema guard did not fire")
    except ValueError:
        pass
    print("self-test passed (offline SQL generation and safety checks; no database queried)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", help="Existing institutional SQL Server ETL config YAML")
    p.add_argument("--stage-d-summary", type=Path, default=DEFAULT_PARENT,
                   help="Original completed Stage D aggregate JSON (required to anchor checks)")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="LOCAL, DISCLOSURE-CONTROLLED aggregate JSON output")
    p.add_argument("--self-test", action="store_true", help="No database access; syntax/safety smoke checks")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return
    if not a.config:
        p.error("--config is required unless --self-test is given")
    run(a.config, a.stage_d_summary, a.output)


if __name__ == "__main__":
    main()