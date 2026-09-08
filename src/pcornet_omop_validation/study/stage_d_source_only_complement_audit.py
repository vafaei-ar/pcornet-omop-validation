from __future__ import annotations

"""Post-outcome audit of the source-only Stage D complement.

This audit is descriptive and was defined after the primary Stage D results were known.
It does not redefine the locked D0 phenotype, Stage D outcome, or frozen ETL. The goal
is to verify patient-by-patient whether the end-to-end risk difference is carried by
source-eligible patients outside the fixed/shared outcome population and to quantify
why those patients are outside that population.
"""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
AUDIT_PATH = Path("study_definitions/stage_d_source_only_complement_audit_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
STAGE_D_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")

# Previously observed Stage C/D results are audit anchors only. They are declared
# explicitly because this is a post-outcome audit, not a new prespecified analysis.
EXPECTED = {
    "d0": {"source": 9815, "omop": 6001, "source_only": 3814},
    "30_day": {
        "source_eligible": 7277,
        "source_events": 1178,
        "omop_eligible": 4374,
        "fixed_eligible": 4374,
        "fixed_source_events": 753,
    },
    "90_day": {
        "source_eligible": 6508,
        "source_events": 1798,
        "omop_eligible": 3822,
        "fixed_eligible": 3822,
        "fixed_source_events": 1132,
    },
}


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


def _sql_list(values: set[str] | frozenset[str]) -> str:
    return ",".join(
        "'" + str(value).replace("'", "''") + "'" for value in sorted(values)
    )


def _risk(events: int, eligible: int) -> float | None:
    return None if eligible == 0 else events / eligible


def _group(con, where: str, event_col: str) -> dict[str, int | float | None]:
    row = con.execute(
        text(
            f"""
            SELECT COUNT_BIG(*) AS eligible,
                   SUM(CASE WHEN {event_col}=1 THEN 1 ELSE 0 END) AS events
            FROM #audit_labels
            WHERE {where}
            """
        )
    ).mappings().one()
    eligible = int(row["eligible"] or 0)
    events = int(row["events"] or 0)
    return {"eligible": eligible, "events": events, "risk": _risk(events, eligible)}


def _count(con, where: str) -> int:
    return int(
        con.execute(
            text(f"SELECT COUNT_BIG(*) FROM #audit_labels WHERE {where}")
        ).scalar_one()
        or 0
    )


def _window_results(con, days: int) -> dict[str, object]:
    source_cov = f"source_covered{days}=1"
    omop_cov = f"omop_covered{days}=1"
    event_col = f"source_event{days}"
    fixed = f"{source_cov} AND omop_member=1 AND {omop_cov} AND index_exact=1"
    complement = (
        f"{source_cov} AND NOT "
        f"(omop_member=1 AND {omop_cov} AND index_exact=1)"
    )

    source = _group(con, source_cov, event_col)
    omop = _group(con, f"omop_member=1 AND {omop_cov}", event_col)
    fixed_group = _group(con, fixed, event_col)
    complement_group = _group(con, complement, event_col)

    fixed_risk = fixed_group["risk"]
    complement_risk = complement_group["risk"]
    risk_difference_pp = (
        None
        if fixed_risk is None or complement_risk is None
        else 100.0 * (complement_risk - fixed_risk)
    )
    risk_ratio = (
        None
        if fixed_risk in (None, 0) or complement_risk is None
        else complement_risk / fixed_risk
    )

    decomposition = {
        "not_in_lineage_faithful_omop_d0": _count(
            con, f"{source_cov} AND omop_member=0"
        ),
        "target_observability_only": _count(
            con, f"{source_cov} AND omop_member=1 AND omop_covered{days}=0"
        ),
        "index_mismatch_only": _count(
            con,
            f"{source_cov} AND omop_member=1 AND {omop_cov} AND index_exact=0",
        ),
    }
    decomposition["sum"] = sum(decomposition.values())

    return {
        "source_end_to_end": source,
        "omop_end_to_end_membership_with_target_observability": omop,
        "fixed_shared": fixed_group,
        "source_only_complement": complement_group,
        "source_only_minus_fixed_risk_difference_percentage_points": risk_difference_pp,
        "source_only_over_fixed_risk_ratio": risk_ratio,
        "complement_decomposition": decomposition,
        "partition_identity_holds": (
            source["eligible"]
            == fixed_group["eligible"] + complement_group["eligible"]
            and source["events"]
            == fixed_group["events"] + complement_group["events"]
        ),
    }


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    audit_definition = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    if (
        audit_definition.get("status")
        != "post_outcome_audit_defined_after_primary_stage_d_results"
    ):
        raise RuntimeError("Unexpected source-only complement audit status")
    if audit_definition.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Audit definition is not anchored to the frozen ETL")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    code_list = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    acute_source = "'ED','EI','IP'"

    out_dir = (
        Path(output_dir)
        if output_dir
        else cfg.audit_dir.parent
        / "publication_analysis"
        / "stage_d_analytical_equivalence"
        / "source_only_complement_audit"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            print("progress: reproducing locked source D0 cohort", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_src_d0') IS NOT NULL "
                "DROP TABLE #audit_src_d0"
            )
            con.exec_driver_sql(
                f"""
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255), d.PATID) AS patid,
                         CONVERT(nvarchar(255), d.ENCOUNTERID) AS encounterid,
                         CONVERT(nvarchar(255), d.DIAGNOSISID) AS diagnosisid,
                         CAST(d.DX_DATE AS date) AS dx_date,
                         ROW_NUMBER() OVER (
                           PARTITION BY CONVERT(nvarchar(255), d.PATID),
                                        CONVERT(nvarchar(255), d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                                    CAST(d.DX_DATE AS date),
                                    {_norm('d.DX')},
                                    CONVERT(nvarchar(255), d.DIAGNOSISID)
                         ) AS rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({code_list})
                    AND {_short('d.PDX')}='P'
                ), encounters AS (
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
                         ) AS index_date,
                         CAST(dm.BIRTH_DATE AS date) AS birth_date
                  FROM dx_rank x
                  JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                    ON CONVERT(nvarchar(255), e.PATID)=x.patid
                   AND CONVERT(nvarchar(255), e.ENCOUNTERID)=x.encounterid
                  JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm
                    ON CONVERT(nvarchar(255), dm.PATID)=x.patid
                  WHERE x.rn=1
                    AND {_short('e.ENC_TYPE')} IN ('EI','IP')
                    AND e.ADMIT_DATE IS NOT NULL
                    AND e.DISCHARGE_DATE IS NOT NULL
                    AND DATEDIFF(
                          day,
                          CAST(e.ADMIT_DATE AS date),
                          CAST(e.DISCHARGE_DATE AS date)
                        ) >= 1
                ), ranked AS (
                  SELECT *,
                         ROW_NUMBER() OVER (
                           PARTITION BY patid ORDER BY index_date, encounterid
                         ) AS patient_rn
                  FROM encounters
                  WHERE index_date IS NOT NULL
                )
                SELECT patid,
                       encounterid,
                       diagnosisid,
                       dx_date,
                       admit_date,
                       discharge_date,
                       index_date,
                       birth_date
                INTO #audit_src_d0
                FROM ranked
                WHERE patient_rn=1
                  AND FLOOR(DATEDIFF(day, birth_date, index_date)/365.0) >= 18;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_src_d0
                  ON #audit_src_d0(patid);
                """
            )

            print(
                "progress: reproducing lineage-faithful OMOP D0 membership",
                flush=True,
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_omop_d0_all') IS NOT NULL "
                "DROP TABLE #audit_omop_d0_all"
            )
            con.exec_driver_sql(
                f"""
                SELECT s.patid,
                       p.person_id,
                       s.encounterid,
                       s.index_date AS source_index_date,
                       CAST(v.visit_end_date AS date) AS omop_discharge_date,
                       COALESCE(
                         CAST(co.condition_start_date AS date),
                         CAST(v.visit_start_date AS date),
                         CAST(v.visit_end_date AS date)
                       ) AS omop_index_date,
                       co.condition_occurrence_id
                INTO #audit_omop_d0_all
                FROM #audit_src_d0 s
                JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(255), p.person_source_value)=s.patid
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx
                  ON CONVERT(nvarchar(255), vx.encounterid)=s.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id=vx.visit_occurrence_id
                 AND v.person_id=p.person_id
                JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx
                  ON cx.source_domain='DIAGNOSIS'
                 AND CONVERT(nvarchar(255), cx.source_record_id)=s.diagnosisid
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id=cx.condition_occurrence_id
                 AND co.person_id=p.person_id
                 AND co.visit_occurrence_id=v.visit_occurrence_id;
                """
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_omop_d0') IS NOT NULL "
                "DROP TABLE #audit_omop_d0"
            )
            con.exec_driver_sql(
                """
                ;WITH ranked AS (
                  SELECT *,
                         ROW_NUMBER() OVER (
                           PARTITION BY patid
                           ORDER BY omop_index_date, condition_occurrence_id
                         ) AS rn
                  FROM #audit_omop_d0_all
                )
                SELECT patid,
                       person_id,
                       source_index_date,
                       omop_discharge_date,
                       omop_index_date
                INTO #audit_omop_d0
                FROM ranked
                WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_omop_d0
                  ON #audit_omop_d0(patid);
                """
            )

            print(
                "progress: materializing source outcomes and both-representation observability",
                flush=True,
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#audit_labels') IS NOT NULL "
                "DROP TABLE #audit_labels"
            )
            con.exec_driver_sql(
                f"""
                SELECT s.patid,
                       s.diagnosisid,
                       s.dx_date,
                       s.discharge_date,
                       CASE WHEN o.patid IS NULL THEN 0 ELSE 1 END AS omop_member,
                       CASE WHEN o.patid IS NOT NULL
                                  AND s.index_date=o.omop_index_date
                            THEN 1 ELSE 0 END AS index_exact,
                       CASE WHEN EXISTS (
                         SELECT 1
                         FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                         WHERE CONVERT(nvarchar(255), en.PATID)=s.patid
                           AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                           AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day, 30, s.discharge_date)
                       ) THEN 1 ELSE 0 END AS source_covered30,
                       CASE WHEN EXISTS (
                         SELECT 1
                         FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                         WHERE CONVERT(nvarchar(255), en.PATID)=s.patid
                           AND CAST(en.ENR_START_DATE AS date)<=s.discharge_date
                           AND CAST(en.ENR_END_DATE AS date)>=DATEADD(day, 90, s.discharge_date)
                       ) THEN 1 ELSE 0 END AS source_covered90,
                       CASE WHEN o.patid IS NOT NULL AND EXISTS (
                         SELECT 1
                         FROM [{target_schema}].[observation_period] op
                         WHERE op.person_id=o.person_id
                           AND CAST(op.observation_period_start_date AS date)<=o.omop_discharge_date
                           AND CAST(op.observation_period_end_date AS date)>=DATEADD(day, 30, o.omop_discharge_date)
                       ) THEN 1 ELSE 0 END AS omop_covered30,
                       CASE WHEN o.patid IS NOT NULL AND EXISTS (
                         SELECT 1
                         FROM [{target_schema}].[observation_period] op
                         WHERE op.person_id=o.person_id
                           AND CAST(op.observation_period_start_date AS date)<=o.omop_discharge_date
                           AND CAST(op.observation_period_end_date AS date)>=DATEADD(day, 90, o.omop_discharge_date)
                       ) THEN 1 ELSE 0 END AS omop_covered90,
                       CASE WHEN EXISTS (
                         SELECT 1
                         FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                         WHERE CONVERT(nvarchar(255), e.PATID)=s.patid
                           AND CAST(e.ADMIT_DATE AS date)>s.discharge_date
                           AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day, 30, s.discharge_date)
                           AND {_short('e.ENC_TYPE')} IN ({acute_source})
                       ) THEN 1 ELSE 0 END AS source_event30,
                       CASE WHEN EXISTS (
                         SELECT 1
                         FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                         WHERE CONVERT(nvarchar(255), e.PATID)=s.patid
                           AND CAST(e.ADMIT_DATE AS date)>s.discharge_date
                           AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day, 90, s.discharge_date)
                           AND {_short('e.ENC_TYPE')} IN ({acute_source})
                       ) THEN 1 ELSE 0 END AS source_event90
                INTO #audit_labels
                FROM #audit_src_d0 s
                LEFT JOIN #audit_omop_d0 o ON o.patid=s.patid;
                CREATE UNIQUE CLUSTERED INDEX IX_audit_labels
                  ON #audit_labels(patid);
                """
            )

            membership = {
                "source": _count(con, "1=1"),
                "omop": _count(con, "omop_member=1"),
                "source_only": _count(con, "omop_member=0"),
                "source_only_selected_diagnosis_date_missing": _count(
                    con, "omop_member=0 AND dx_date IS NULL"
                ),
                "source_only_selected_diagnosis_date_recorded": _count(
                    con, "omop_member=0 AND dx_date IS NOT NULL"
                ),
            }

            if (
                membership["source"] != EXPECTED["d0"]["source"]
                or membership["omop"] != EXPECTED["d0"]["omop"]
                or membership["source_only"] != EXPECTED["d0"]["source_only"]
            ):
                raise RuntimeError(
                    "Audit failed to reproduce the locked D0 membership anchors: "
                    f"observed={membership}, expected={EXPECTED['d0']}"
                )

            windows = {
                "30_day": _window_results(con, 30),
                "90_day": _window_results(con, 90),
            }
            for key, days in (("30_day", 30), ("90_day", 90)):
                observed = windows[key]
                expected = EXPECTED[key]
                source = observed["source_end_to_end"]
                omop = observed[
                    "omop_end_to_end_membership_with_target_observability"
                ]
                fixed_group = observed["fixed_shared"]
                if (
                    source["eligible"] != expected["source_eligible"]
                    or source["events"] != expected["source_events"]
                    or omop["eligible"] != expected["omop_eligible"]
                    or fixed_group["eligible"] != expected["fixed_eligible"]
                    or fixed_group["events"] != expected["fixed_source_events"]
                ):
                    raise RuntimeError(
                        f"Audit failed to reproduce locked Stage D {days}-day anchors: "
                        f"observed={observed}, expected={expected}"
                    )
                if not observed["partition_identity_holds"]:
                    raise RuntimeError(
                        f"{days}-day source/fixed/complement partition failed"
                    )
                if (
                    observed["complement_decomposition"]["sum"]
                    != observed["source_only_complement"]["eligible"]
                ):
                    raise RuntimeError(
                        f"{days}-day complement mechanism decomposition failed"
                    )

            payload: dict[str, object] = {
                "status": "stage_d_source_only_complement_audit_complete",
                "audit_role": "post_outcome_descriptive_audit",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "frozen_etl_sha": FROZEN_ETL_SHA,
                "analysis_git_sha": _git("rev-parse", "HEAD"),
                "audit_definition": str(AUDIT_PATH),
                "audit_definition_sha256": _sha256(AUDIT_PATH),
                "inherited_d0_definition_sha256": _sha256(D0_PATH),
                "inherited_stage_d_definition_sha256": _sha256(STAGE_D_PATH),
                "primary_stage_d_results_were_known_before_this_audit": True,
                "membership": membership,
                "windows": windows,
                "interpretation_guardrail": (
                    "This audit can demonstrate outcome-associated selective cohort "
                    "loss, but it does not classify the missingness mechanism as "
                    "MCAR/MAR/MNAR and does not estimate a causal selection effect."
                ),
            }
            output_path = out_dir / "stage_d_source_only_complement_audit.json"
            output_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit outcome risk in the source-only Stage D complement"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    result = run(args.config, args.output_dir)
    print("status:", result["status"])
    print(json.dumps(result["membership"], indent=2, sort_keys=True))
    print(json.dumps(result["windows"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
