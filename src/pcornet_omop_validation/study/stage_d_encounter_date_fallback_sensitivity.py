from __future__ import annotations

"""Reviewer-inspired post-freeze Stage D encounter-date fallback sensitivity.

This module executes the Stage D component already locked in
``study_definitions/stage_c_d_encounter_date_fallback_sensitivity_v1.json``.
It reconstructs the unchanged source-faithful D0 cohort and the independently
selected non-destructive fallback-target D0 cohort using the same alternative target
diagnosis-date policy as the completed Stage C fallback sensitivity.  It then applies
the locked 30-day and 90-day acute-care outcome definitions and representation-
specific observability rules without altering the frozen OMOP target.

The source outcome anchors were known and committed before this fallback analysis.
Fallback-target outcome counts are deliberately not prespecified.
"""

import argparse
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
PRIMARY_STAGE_D_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")
STAGE_C_RESULT_PATH = Path(
    "results/publication_analysis/stage_c_phenotypes/encounter_date_fallback_sensitivity/"
    "stage_c_encounter_date_fallback_sensitivity.json"
)
EXPECTED_SOURCE_D0 = 9815
EXPECTED_SOURCE_OUTCOME = {
    30: {"eligible": 7277, "events": 1178},
    90: {"eligible": 6508, "events": 1798},
}
ACUTE_TARGET_CONCEPTS = (9203, 262, 9201)


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
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
    return (
        "REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), "
        + expr
        + ")))),'.','')"
    )


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _risk(events: int, eligible: int) -> float | None:
    return None if eligible == 0 else events / eligible


def _compare(
    source_eligible: int,
    source_events: int,
    target_eligible: int,
    target_events: int,
    abs_margin_pp: float,
    rr_lo: float,
    rr_hi: float,
) -> dict[str, Any]:
    sr = _risk(source_events, source_eligible)
    tr = _risk(target_events, target_eligible)
    diff = None if sr is None or tr is None else 100.0 * (tr - sr)
    rr = None if sr in (None, 0) or tr is None else tr / sr
    abs_ok = None if diff is None else abs(diff) <= abs_margin_pp
    rr_ok = None if rr is None else rr_lo <= rr <= rr_hi
    return {
        "source_eligible": source_eligible,
        "source_events": source_events,
        "source_risk": sr,
        "fallback_target_eligible": target_eligible,
        "fallback_target_events": target_events,
        "fallback_target_risk": tr,
        "absolute_risk_difference_percentage_points": diff,
        "relative_risk_ratio_fallback_over_source": rr,
        "absolute_margin_percentage_points": abs_margin_pp,
        "relative_margin": [rr_lo, rr_hi],
        "absolute_margin_met": abs_ok,
        "relative_margin_met": rr_ok,
        "both_margins_met": (
            None if abs_ok is None or rr_ok is None else bool(abs_ok and rr_ok)
        ),
    }


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    primary_stage_d = json.loads(PRIMARY_STAGE_D_PATH.read_text(encoding="utf-8"))

    if study.get("status") != (
        "reviewer_inspired_post_freeze_sensitivity_locked_before_fallback_execution"
    ):
        raise RuntimeError("Fallback sensitivity was not locked before execution")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Fallback sensitivity is not anchored to the frozen ETL")
    if primary_stage_d.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Primary Stage D definition is not anchored to the frozen ETL")

    if not STAGE_C_RESULT_PATH.exists():
        raise RuntimeError(
            "Completed Stage C fallback result is required before Stage D fallback analysis"
        )
    stage_c = json.loads(STAGE_C_RESULT_PATH.read_text(encoding="utf-8"))
    if stage_c.get("status") != "stage_c_encounter_date_fallback_sensitivity_complete":
        raise RuntimeError("Stage C fallback result is not complete")
    if stage_c.get("study_definition_sha256") != _sha256(STUDY_PATH):
        raise RuntimeError("Stage C fallback result does not match the current locked sensitivity")
    d0_stage_c = stage_c.get("results", {}).get("D0", {})
    if (
        int(d0_stage_c.get("source_patients", -1)) != EXPECTED_SOURCE_D0
        or int(d0_stage_c.get("fallback_target_patients", -1)) != EXPECTED_SOURCE_D0
        or int(d0_stage_c.get("intersection_patients", -1)) != EXPECTED_SOURCE_D0
        or int(d0_stage_c.get("exact_date_patients", -1)) != EXPECTED_SOURCE_D0
    ):
        raise RuntimeError(f"Stage C fallback D0 anchor is not exact: {d0_stage_c}")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    stroke_codes = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    acute_source = "'ED','EI','IP'"
    acute_target = ",".join(str(x) for x in ACUTE_TARGET_CONCEPTS)

    out_dir = (
        Path(output_dir)
        if output_dir
        else cfg.audit_dir.parent
        / "publication_analysis"
        / "stage_d_analytical_equivalence"
        / "encounter_date_fallback_sensitivity"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            print("progress: materializing unchanged source-faithful D0 candidates", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_candidates') IS NOT NULL DROP TABLE #fd_candidates"
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
                  WHERE {_norm('d.DX')} IN ({stroke_codes})
                    AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,
                       CAST(e.ADMIT_DATE AS date) AS admit_date,
                       CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
                       COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) AS source_index_date,
                       CAST(dm.BIRTH_DATE AS date) AS source_birth_date
                INTO #fd_candidates
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
                CREATE INDEX IX_fd_candidates_patient
                  ON #fd_candidates(patid,source_index_date,encounterid);
                """
            )

            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_src_d0') IS NOT NULL DROP TABLE #fd_src_d0"
            )
            con.exec_driver_sql(
                """
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER(
                    PARTITION BY patid ORDER BY source_index_date,encounterid
                  ) AS rn
                  FROM #fd_candidates
                )
                SELECT patid,encounterid,diagnosisid,dx_date,admit_date,discharge_date,
                       source_index_date AS index_date
                INTO #fd_src_d0
                FROM q
                WHERE rn=1
                  AND FLOOR(DATEDIFF(day,source_birth_date,source_index_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_fd_src_d0 ON #fd_src_d0(patid);
                """
            )

            print("progress: constructing fallback-target D0 overlay", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_existing_condition') IS NOT NULL DROP TABLE #fd_existing_condition"
            )
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
                  FROM #fd_candidates c
                  JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                    ON cx.source_domain='DIAGNOSIS'
                   AND CONVERT(nvarchar(255),cx.source_record_id)=c.diagnosisid
                  JOIN [{target_schema}].[condition_occurrence] co
                    ON co.condition_occurrence_id=cx.condition_occurrence_id
                )
                SELECT patid,encounterid,diagnosisid,condition_start_date,
                       condition_occurrence_id
                INTO #fd_existing_condition
                FROM q WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_fd_existing_condition
                  ON #fd_existing_condition(patid,encounterid,diagnosisid);
                """
            )

            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_target_base') IS NOT NULL DROP TABLE #fd_target_base"
            )
            con.exec_driver_sql(
                f"""
                SELECT c.patid,c.encounterid,c.diagnosisid,c.dx_date,
                       p.person_id,CAST(p.birth_datetime AS date) AS target_birth_date,
                       v.visit_occurrence_id,
                       CAST(v.visit_start_date AS date) AS target_admit_date,
                       CAST(v.visit_end_date AS date) AS target_discharge_date,
                       CASE WHEN c.dx_date IS NOT NULL THEN ec.condition_start_date
                            ELSE CAST(v.visit_start_date AS date) END AS fallback_index_date,
                       CASE WHEN c.dx_date IS NOT NULL THEN 'recorded_diagnosis_date'
                            ELSE 'encounter_admission_date_fallback' END AS date_basis
                INTO #fd_target_base
                FROM #fd_candidates c
                JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(255),p.person_source_value)=c.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                  ON CONVERT(nvarchar(255),vx.encounterid)=c.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id=vx.visit_occurrence_id
                 AND v.person_id=p.person_id
                LEFT JOIN #fd_existing_condition ec
                  ON ec.patid=c.patid
                 AND ec.encounterid=c.encounterid
                 AND ec.diagnosisid=c.diagnosisid
                WHERE (c.dx_date IS NULL OR ec.condition_occurrence_id IS NOT NULL)
                  AND (c.dx_date IS NULL OR ec.condition_start_date IS NOT NULL);
                CREATE INDEX IX_fd_target_base
                  ON #fd_target_base(patid,fallback_index_date,encounterid);
                """
            )

            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_tgt_d0') IS NOT NULL DROP TABLE #fd_tgt_d0"
            )
            con.exec_driver_sql(
                """
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER(
                    PARTITION BY patid
                    ORDER BY fallback_index_date,encounterid,diagnosisid
                  ) AS rn
                  FROM #fd_target_base
                  WHERE fallback_index_date IS NOT NULL
                )
                SELECT patid,person_id,encounterid,diagnosisid,dx_date,
                       target_admit_date,target_discharge_date,
                       fallback_index_date AS index_date,date_basis,visit_occurrence_id
                INTO #fd_tgt_d0
                FROM q
                WHERE rn=1
                  AND FLOOR(DATEDIFF(day,target_birth_date,fallback_index_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_fd_tgt_d0 ON #fd_tgt_d0(patid);
                """
            )

            d0_row = con.execute(
                text(
                    """
                    WITH s AS (SELECT patid,index_date FROM #fd_src_d0),
                         t AS (SELECT patid,index_date FROM #fd_tgt_d0),
                         u AS (SELECT patid FROM s UNION SELECT patid FROM t)
                    SELECT
                      (SELECT COUNT_BIG(*) FROM s) source_patients,
                      (SELECT COUNT_BIG(*) FROM t) fallback_target_patients,
                      SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NOT NULL THEN 1 ELSE 0 END) intersection_patients,
                      SUM(CASE WHEN s.patid IS NOT NULL AND t.patid IS NOT NULL AND s.index_date=t.index_date THEN 1 ELSE 0 END) exact_index_patients
                    FROM u
                    LEFT JOIN s ON s.patid=u.patid
                    LEFT JOIN t ON t.patid=u.patid
                    """
                )
            ).mappings().one()
            d0_reproduction = {k: int(v or 0) for k, v in dict(d0_row).items()}
            if any(
                d0_reproduction[k] != EXPECTED_SOURCE_D0
                for k in (
                    "source_patients",
                    "fallback_target_patients",
                    "intersection_patients",
                    "exact_index_patients",
                )
            ):
                raise RuntimeError(f"Fallback D0 failed Stage C anchor: {d0_reproduction}")

            prov_rows = con.execute(
                text(
                    """
                    SELECT date_basis,COUNT_BIG(*) AS n
                    FROM #fd_tgt_d0
                    GROUP BY date_basis
                    ORDER BY date_basis
                    """
                )
            ).mappings().all()
            d0_provenance = {str(r["date_basis"]): int(r["n"]) for r in prov_rows}
            expected_prov = stage_c.get("selected_target_episode_date_provenance", {}).get(
                "D0", {}
            )
            if d0_provenance != {str(k): int(v) for k, v in expected_prov.items()}:
                raise RuntimeError(
                    f"Fallback D0 provenance differs from completed Stage C: "
                    f"observed={d0_provenance} expected={expected_prov}"
                )

            print("progress: materializing locked 30-day and 90-day outcome labels", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_src_labels') IS NOT NULL DROP TABLE #fd_src_labels"
            )
            con.exec_driver_sql(
                f"""
                SELECT s.*,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                    WHERE CONVERT(nvarchar(255),en.PATID)=s.patid
                      AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                      AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,30,s.discharge_date)
                  ) THEN 1 ELSE 0 END covered30,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                    WHERE CONVERT(nvarchar(255),en.PATID)=s.patid
                      AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                      AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day,90,s.discharge_date)
                  ) THEN 1 ELSE 0 END covered90,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                    WHERE CONVERT(nvarchar(255),e.PATID)=s.patid
                      AND CAST(e.ADMIT_DATE AS date)>s.discharge_date
                      AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,30,s.discharge_date)
                      AND {_short('e.ENC_TYPE')} IN ({acute_source})
                  ) THEN 1 ELSE 0 END event30,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                    WHERE CONVERT(nvarchar(255),e.PATID)=s.patid
                      AND CAST(e.ADMIT_DATE AS date)>s.discharge_date
                      AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,s.discharge_date)
                      AND {_short('e.ENC_TYPE')} IN ({acute_source})
                  ) THEN 1 ELSE 0 END event90
                INTO #fd_src_labels
                FROM #fd_src_d0 s;
                CREATE UNIQUE CLUSTERED INDEX IX_fd_src_labels ON #fd_src_labels(patid);
                """
            )

            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#fd_tgt_labels') IS NOT NULL DROP TABLE #fd_tgt_labels"
            )
            con.exec_driver_sql(
                f"""
                SELECT t.*,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{target_schema}].[observation_period] op
                    WHERE op.person_id=t.person_id
                      AND op.observation_period_start_date<=t.target_discharge_date
                      AND op.observation_period_end_date>=DATEADD(day,30,t.target_discharge_date)
                  ) THEN 1 ELSE 0 END covered30,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{target_schema}].[observation_period] op
                    WHERE op.person_id=t.person_id
                      AND op.observation_period_start_date<=t.target_discharge_date
                      AND op.observation_period_end_date>=DATEADD(day,90,t.target_discharge_date)
                  ) THEN 1 ELSE 0 END covered90,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{target_schema}].[visit_occurrence] v
                    WHERE v.person_id=t.person_id
                      AND CAST(v.visit_start_date AS date)>t.target_discharge_date
                      AND CAST(v.visit_start_date AS date)<=DATEADD(day,30,t.target_discharge_date)
                      AND v.visit_concept_id IN ({acute_target})
                  ) THEN 1 ELSE 0 END event30,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM [{target_schema}].[visit_occurrence] v
                    WHERE v.person_id=t.person_id
                      AND CAST(v.visit_start_date AS date)>t.target_discharge_date
                      AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,t.target_discharge_date)
                      AND v.visit_concept_id IN ({acute_target})
                  ) THEN 1 ELSE 0 END event90
                INTO #fd_tgt_labels
                FROM #fd_tgt_d0 t;
                CREATE UNIQUE CLUSTERED INDEX IX_fd_tgt_labels ON #fd_tgt_labels(patid);
                """
            )

            print("progress: computing fallback Stage D end-to-end estimates", flush=True)
            results: dict[str, Any] = {}
            tolerances = study["stage_d_analysis"][
                "empirical_tolerances_inherited_not_redefined"
            ]
            abs_margin = float(tolerances["absolute_risk_difference_percentage_points"])
            rr_lo = float(tolerances["relative_risk_ratio_lower"])
            rr_hi = float(tolerances["relative_risk_ratio_upper"])

            for window in (30, 90):
                src = con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*) AS eligible,
                               SUM(CASE WHEN event{window}=1 THEN 1 ELSE 0 END) AS events
                        FROM #fd_src_labels
                        WHERE covered{window}=1
                        """
                    )
                ).mappings().one()
                tgt = con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*) AS eligible,
                               SUM(CASE WHEN event{window}=1 THEN 1 ELSE 0 END) AS events
                        FROM #fd_tgt_labels
                        WHERE covered{window}=1
                        """
                    )
                ).mappings().one()
                se = int(src["eligible"] or 0)
                sn = int(src["events"] or 0)
                te = int(tgt["eligible"] or 0)
                tn = int(tgt["events"] or 0)
                expected = EXPECTED_SOURCE_OUTCOME[window]
                if se != expected["eligible"] or sn != expected["events"]:
                    raise RuntimeError(
                        f"Source {window}-day Stage D anchor mismatch: "
                        f"observed={{'eligible': {se}, 'events': {sn}}} expected={expected}"
                    )
                results[f"{window}_day"] = _compare(
                    se, sn, te, tn, abs_margin, rr_lo, rr_hi
                )

            jointly_observable: dict[str, Any] = {}
            for window in (30, 90):
                row = con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*) AS eligible,
                               SUM(CASE WHEN s.event{window}=t.event{window} THEN 1 ELSE 0 END) AS label_agreement,
                               SUM(CASE WHEN s.event{window}=1 AND t.event{window}=1 THEN 1 ELSE 0 END) AS both_positive,
                               SUM(CASE WHEN s.event{window}=1 AND t.event{window}=0 THEN 1 ELSE 0 END) AS source_only_positive,
                               SUM(CASE WHEN s.event{window}=0 AND t.event{window}=1 THEN 1 ELSE 0 END) AS fallback_only_positive,
                               SUM(CASE WHEN s.event{window}=0 AND t.event{window}=0 THEN 1 ELSE 0 END) AS both_negative
                        FROM #fd_src_labels s
                        JOIN #fd_tgt_labels t ON t.patid=s.patid
                        WHERE s.index_date=t.index_date
                          AND s.covered{window}=1
                          AND t.covered{window}=1
                        """
                    )
                ).mappings().one()
                jointly_observable[f"{window}_day"] = {
                    k: int(v or 0) for k, v in dict(row).items()
                }
    finally:
        engine.dispose()

    payload: dict[str, object] = {
        "status": "stage_d_encounter_date_fallback_sensitivity_complete",
        "analysis_role": "reviewer_inspired_post_freeze_alternative_target_date_policy_sensitivity",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "study_definition": str(STUDY_PATH),
        "study_definition_sha256": _sha256(STUDY_PATH),
        "primary_stage_d_definition": str(PRIMARY_STAGE_D_PATH),
        "primary_stage_d_definition_sha256": _sha256(PRIMARY_STAGE_D_PATH),
        "stage_c_fallback_result": str(STAGE_C_RESULT_PATH),
        "stage_c_fallback_result_sha256": _sha256(STAGE_C_RESULT_PATH),
        "d0_reproduction": d0_reproduction,
        "selected_fallback_d0_date_provenance": d0_provenance,
        "end_to_end_results": results,
        "jointly_observable_label_check": jointly_observable,
        "interpretation_guardrail": (
            "This post-freeze reviewer-inspired sensitivity changes only target diagnosis-date "
            "eligibility/date assignment for D0 construction. The frozen OMOP database, Stage D "
            "acute-care outcome rules, observability rules, and empirical cross-CDM reproducibility "
            "tolerances are unchanged. Convergence does not establish the fallback policy as the "
            "uniquely correct or standard ETL."
        ),
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "frozen_target_modified": False,
            "status": "passed",
        },
    }

    output_path = out_dir / "stage_d_encounter_date_fallback_sensitivity.json"
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("status:", payload["status"])
    print("D0 reproduction:")
    print(json.dumps(d0_reproduction, indent=2, sort_keys=True))
    print("end-to-end results:")
    print(json.dumps(results, indent=2, sort_keys=True))
    print("jointly observable label check:")
    print(json.dumps(jointly_observable, indent=2, sort_keys=True))
    print("output:", output_path)
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage D encounter-date fallback sensitivity")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    args = p.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
