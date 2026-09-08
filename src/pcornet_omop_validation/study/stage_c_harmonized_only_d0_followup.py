from __future__ import annotations

"""Post-result follow-up for the single harmonized-only source D0 patient.

The transition audit established one patient who entered the harmonized source D0
cohort despite not being present in the primary source D0 cohort. This module explains
that edge case using aggregate counts only. It reconstructs the same D0 candidate
ranking twice and inspects the episode selected by the primary ranking *before* the
adult-age filter. No patient identifier is written to disk or printed.
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
AUDIT_PATH = Path("study_definitions/stage_c_harmonized_only_d0_followup_v1.json")


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


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    cfg = load_etl_config(config_path)
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    if audit.get("status") != "post_result_descriptive_followup_defined_after_transition_audit":
        raise RuntimeError("Unexpected D0 harmonized-only follow-up status")
    if audit.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Follow-up definition is not anchored to the frozen ETL")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    stroke_codes = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            print("progress: materializing D0 candidate episodes", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#d0_followup_candidates') IS NOT NULL "
                "DROP TABLE #d0_followup_candidates"
            )
            con.exec_driver_sql(
                f"""
                ;WITH dx_rank AS (
                  SELECT
                    CONVERT(nvarchar(255),d.PATID) AS patid,
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
                SELECT
                  x.patid,
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
                  CAST(dm.BIRTH_DATE AS date) AS birth_date
                INTO #d0_followup_candidates
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
                CREATE INDEX IX_d0_followup_candidates
                  ON #d0_followup_candidates(patid,source_index_date,encounterid);
                """
            )

            print("progress: selecting primary episode before and after adult filter", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#d0_primary_pre_age') IS NOT NULL "
                "DROP TABLE #d0_primary_pre_age"
            )
            con.exec_driver_sql(
                """
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER (
                    PARTITION BY patid ORDER BY source_index_date,encounterid
                  ) AS rn
                  FROM #d0_followup_candidates
                )
                SELECT
                  patid,encounterid,diagnosisid,dx_date,source_index_date AS index_date,
                  birth_date,
                  CASE WHEN FLOOR(DATEDIFF(day,birth_date,source_index_date)/365.0)>=18
                       THEN 1 ELSE 0 END AS adult_flag
                INTO #d0_primary_pre_age
                FROM q WHERE rn=1;
                CREATE UNIQUE CLUSTERED INDEX IX_d0_primary_pre_age
                  ON #d0_primary_pre_age(patid);
                """
            )
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#d0_primary') IS NOT NULL DROP TABLE #d0_primary"
            )
            con.exec_driver_sql(
                """
                SELECT patid,encounterid,diagnosisid,dx_date,index_date
                INTO #d0_primary
                FROM #d0_primary_pre_age
                WHERE adult_flag=1;
                CREATE UNIQUE CLUSTERED INDEX IX_d0_primary ON #d0_primary(patid);
                """
            )

            print("progress: selecting harmonized D0 episode", flush=True)
            con.exec_driver_sql(
                "IF OBJECT_ID('tempdb..#d0_harmonized') IS NOT NULL DROP TABLE #d0_harmonized"
            )
            con.exec_driver_sql(
                """
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER (
                    PARTITION BY patid ORDER BY dx_date,encounterid
                  ) AS rn
                  FROM #d0_followup_candidates
                  WHERE dx_date IS NOT NULL
                )
                SELECT patid,encounterid,diagnosisid,dx_date,dx_date AS index_date
                INTO #d0_harmonized
                FROM q
                WHERE rn=1
                  AND FLOOR(DATEDIFF(day,birth_date,dx_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_d0_harmonized ON #d0_harmonized(patid);
                """
            )

            anchors = con.execute(
                text(
                    """
                    SELECT
                      (SELECT COUNT_BIG(*) FROM #d0_primary) AS primary_patients,
                      (SELECT COUNT_BIG(*) FROM #d0_harmonized) AS harmonized_patients,
                      (SELECT COUNT_BIG(*)
                         FROM #d0_harmonized h
                         LEFT JOIN #d0_primary p ON p.patid=h.patid
                        WHERE p.patid IS NULL) AS harmonized_only_patients
                    """
                )
            ).mappings().one()
            anchors = {key: int(value or 0) for key, value in dict(anchors).items()}
            expected = {
                "primary_patients": 9815,
                "harmonized_patients": 6198,
                "harmonized_only_patients": 1,
            }
            if anchors != expected:
                raise RuntimeError(f"D0 follow-up anchor mismatch: {anchors}")

            print("progress: classifying harmonized-only mechanism", flush=True)
            mechanism = con.execute(
                text(
                    """
                    WITH ho AS (
                      SELECT h.*
                      FROM #d0_harmonized h
                      LEFT JOIN #d0_primary p ON p.patid=h.patid
                      WHERE p.patid IS NULL
                    )
                    SELECT
                      COUNT_BIG(*) AS harmonized_only_patients,
                      SUM(CASE WHEN pre.patid IS NOT NULL THEN 1 ELSE 0 END)
                        AS primary_pre_age_episode_present,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND pre.adult_flag=0 THEN 1 ELSE 0 END)
                        AS primary_pre_age_episode_under18,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND pre.adult_flag=1 THEN 1 ELSE 0 END)
                        AS primary_pre_age_episode_adult,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND pre.dx_date IS NULL THEN 1 ELSE 0 END)
                        AS primary_pre_age_episode_diagnosis_date_missing,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND pre.dx_date IS NOT NULL THEN 1 ELSE 0 END)
                        AS primary_pre_age_episode_diagnosis_date_recorded,
                      SUM(CASE WHEN pre.patid IS NOT NULL
                                AND (pre.encounterid<>ho.encounterid OR pre.diagnosisid<>ho.diagnosisid)
                               THEN 1 ELSE 0 END)
                        AS harmonized_reselected_to_different_episode,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND ho.index_date>pre.index_date THEN 1 ELSE 0 END)
                        AS harmonized_episode_later,
                      SUM(CASE WHEN pre.patid IS NOT NULL AND ho.index_date=pre.index_date THEN 1 ELSE 0 END)
                        AS harmonized_same_index_date,
                      SUM(CASE WHEN ho.dx_date IS NOT NULL THEN 1 ELSE 0 END)
                        AS harmonized_selected_diagnosis_date_recorded
                    FROM ho
                    LEFT JOIN #d0_primary_pre_age pre ON pre.patid=ho.patid
                    """
                )
            ).mappings().one()
            mechanism = {key: int(value or 0) for key, value in dict(mechanism).items()}
    finally:
        engine.dispose()

    payload: dict[str, object] = {
        "status": "stage_c_harmonized_only_d0_followup_complete",
        "audit_role": "post_result_descriptive_followup",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "audit_definition": str(AUDIT_PATH),
        "audit_definition_sha256": _sha256(AUDIT_PATH),
        "anchors": anchors,
        "harmonized_only_mechanism": mechanism,
        "interpretation_guardrail": (
            "This follow-up explains the harmonized-only D0 edge case using aggregate "
            "counts only. It does not alter the frozen ETL or either Stage C cohort definition."
        ),
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    out_dir = (
        Path(output_dir)
        if output_dir
        else cfg.audit_dir.parent
        / "publication_analysis"
        / "stage_c_phenotypes"
        / "harmonized_only_d0_followup"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "stage_c_harmonized_only_d0_followup.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("status:", payload["status"])
    print(json.dumps(mechanism, indent=2, sort_keys=True))
    print("output:", output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explain the single harmonized-only source D0 patient using aggregate counts"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
