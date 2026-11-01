#!/usr/bin/env python3
"""Read-only analysis-210 overlap against the LOCKED Stage C/D D0 cohort.

Uses the existing private four-ID Achilles result and the frozen read-only
QC login. Never retrieves IDs or individual visits; SQL returns aggregates
only. Stores all small counts locally as mode 0600. Does not rerun Achilles.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_postfreeze_achilles_exception_counts import (  # noqa: E402
    PRIVATE_DIR,
    PRIVATE_OUTPUT,
    THRESHOLD,
    verify_existing_private_output,
)
from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    FROZEN_DATABASE,
    ROOT,
    inspect_frozen,
    make_restricted_odbc_string,
    verified_server_config,
    verify_identity,
    verify_manifest,
)
from pcornet_omop_validation.study.stroke_codes import (  # noqa: E402
    ICD9_STROKE_CODES,
    ICD10_STROKE_CODES,
)

OUTPUT = PRIVATE_DIR / "analysis_210_locked_d0_overlap.json"
EXPECTED_SOURCE_D0 = 9815
EXPECTED_OMOP_D0 = 6001
COLUMNS = (
    "source_d0_patients",
    "omop_lineage_d0_patients",
    "exception_visits",
    "exception_visits_on_source_d0_patients",
    "exception_visits_on_omop_d0_patients",
    "exception_index_visits_on_omop_d0",
    "exception_acute_visits_30d_on_omop_d0",
    "exception_acute_visits_90d_on_omop_d0",
    "exception_omop_d0_patients",
)


def _sql_list(values: frozenset[str]) -> str:
    if not values or any(not s.isalnum() for s in values):
        raise AuditBlocked("Unexpected locked stroke code set")
    return ",".join("'" + s + "'" for s in sorted(values))


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255),{expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50),{expr}))))"


def overlap_sql() -> str:
    """Mirror Stage C/D source-index ranking and lineage-faithful OMOP selection.

    Unlike original stage scripts, use CTEs instead of SELECT INTO temp tables.
    Analysis 210 is equivalent to NOT EXISTS for the visit-start-date condition.
    """
    codes = _sql_list(ICD9_STROKE_CODES | ICD10_STROKE_CODES)
    return f"""
    ;WITH dx_rank AS (
        SELECT CONVERT(nvarchar(255), d.PATID) AS patid,
               CONVERT(nvarchar(255), d.ENCOUNTERID) AS encounterid,
               CONVERT(nvarchar(255), d.DIAGNOSISID) AS diagnosisid,
               CAST(d.DX_DATE AS date) AS dx_date,
               {_norm("d.DX")} AS dx_norm,
               ROW_NUMBER() OVER (
                 PARTITION BY CONVERT(nvarchar(255), d.PATID),
                              CONVERT(nvarchar(255), d.ENCOUNTERID)
                 ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,
                   CAST(d.DX_DATE AS date), {_norm("d.DX")},
                   CONVERT(nvarchar(255), d.DIAGNOSISID)
               ) AS rn
        FROM [dbo].[PCORnet_DIAGNOSIS] d
        WHERE {_norm("d.DX")} IN ({codes})
          AND {_short("d.PDX")} = 'P'
    ), encounters AS (
        SELECT x.patid, x.encounterid, x.diagnosisid, x.dx_date,
               CAST(e.ADMIT_DATE AS date) AS admit_date,
               CAST(e.DISCHARGE_DATE AS date) AS discharge_date,
               COALESCE(x.dx_date, CAST(e.ADMIT_DATE AS date),
                        CAST(e.DISCHARGE_DATE AS date)) AS index_date,
               CAST(dm.BIRTH_DATE AS date) AS birth_date
        FROM dx_rank x
        JOIN [dbo].[PCORnet_ENCOUNTER] e
          ON CONVERT(nvarchar(255),e.PATID)=x.patid
         AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
        JOIN [dbo].[PCORnet_DEMOGRAPHIC] dm
          ON CONVERT(nvarchar(255),dm.PATID)=x.patid
        WHERE x.rn=1 AND {_short("e.ENC_TYPE")} IN ('EI','IP')
          AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
          AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),
                           CAST(e.DISCHARGE_DATE AS date))>=1
    ), ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (
                 PARTITION BY patid ORDER BY index_date,encounterid
               ) AS patient_rn
        FROM encounters WHERE index_date IS NOT NULL
    ), source_d0 AS (
        SELECT patid, encounterid, diagnosisid, index_date
        FROM ranked
        WHERE patient_rn=1
          AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18
    ), omop_d0_all AS (
        SELECT s.patid,p.person_id,s.encounterid,
               CAST(v.visit_end_date AS date) AS omop_discharge_date,
               COALESCE(CAST(co.condition_start_date AS date),
                        CAST(v.visit_start_date AS date),
                        CAST(v.visit_end_date AS date)) AS omop_index_date,
               v.visit_occurrence_id,co.condition_occurrence_id
        FROM source_d0 s
        JOIN [dbo].[person] p
          ON CONVERT(nvarchar(255),p.person_source_value)=s.patid
        JOIN [dbo].[etl_visit_occurrence_xwalk] vx
          ON CONVERT(nvarchar(255),vx.encounterid)=s.encounterid
        JOIN [dbo].[visit_occurrence] v
          ON v.visit_occurrence_id=vx.visit_occurrence_id
         AND v.person_id=p.person_id
        JOIN [dbo].[etl_condition_occurrence_xwalk] cx
          ON cx.source_domain='DIAGNOSIS'
         AND CONVERT(nvarchar(255),cx.source_record_id)=s.diagnosisid
        JOIN [dbo].[condition_occurrence] co
          ON co.condition_occurrence_id=cx.condition_occurrence_id
         AND co.person_id=p.person_id
         AND co.visit_occurrence_id=v.visit_occurrence_id
    ), omop_d0_ranked AS (
        SELECT *,
               ROW_NUMBER() OVER(
                 PARTITION BY patid
                 ORDER BY omop_index_date,condition_occurrence_id
               ) AS rn
        FROM omop_d0_all
    ), omop_d0 AS (
        SELECT person_id,patid,visit_occurrence_id,omop_discharge_date
        FROM omop_d0_ranked WHERE rn=1
    ), bad_visits AS (
        SELECT vo.person_id,vo.visit_occurrence_id,
               vo.visit_start_date,vo.visit_concept_id
        FROM [dbo].[visit_occurrence] vo
        WHERE NOT EXISTS (
            SELECT 1 FROM [dbo].[observation_period] op
            WHERE op.person_id=vo.person_id
              AND vo.visit_start_date>=op.observation_period_start_date
              AND vo.visit_start_date<=op.observation_period_end_date
        )
    )
    SELECT
      (SELECT COUNT_BIG(*) FROM source_d0) AS source_d0_patients,
      (SELECT COUNT_BIG(*) FROM omop_d0) AS omop_lineage_d0_patients,
      COUNT_BIG(*) AS exception_visits,
      COUNT_BIG(CASE WHEN s.patid IS NOT NULL THEN 1 END)
        AS exception_visits_on_source_d0_patients,
      COUNT_BIG(CASE WHEN o.person_id IS NOT NULL THEN 1 END)
        AS exception_visits_on_omop_d0_patients,
      COUNT_BIG(CASE WHEN o.visit_occurrence_id=b.visit_occurrence_id THEN 1 END)
        AS exception_index_visits_on_omop_d0,
      COUNT_BIG(CASE WHEN o.person_id IS NOT NULL
                AND b.visit_start_date>o.omop_discharge_date
                AND b.visit_start_date<=DATEADD(day,30,o.omop_discharge_date)
                AND b.visit_concept_id IN (9203,262,9201)
                THEN 1 END)
        AS exception_acute_visits_30d_on_omop_d0,
      COUNT_BIG(CASE WHEN o.person_id IS NOT NULL
                AND b.visit_start_date>o.omop_discharge_date
                AND b.visit_start_date<=DATEADD(day,90,o.omop_discharge_date)
                AND b.visit_concept_id IN (9203,262,9201)
                THEN 1 END)
        AS exception_acute_visits_90d_on_omop_d0,
      COUNT_BIG(DISTINCT CASE WHEN o.person_id IS NOT NULL
                              THEN b.person_id END)
        AS exception_omop_d0_patients
    FROM bad_visits b
    LEFT JOIN [dbo].[person] p ON p.person_id=b.person_id
    LEFT JOIN source_d0 s
      ON s.patid=CONVERT(nvarchar(255),p.person_source_value)
    LEFT JOIN omop_d0 o ON o.person_id=b.person_id
    """


def validate_private_metadata() -> int:
    """Validate and read the already protected four-analysis JSON locally."""
    verify_existing_private_output()
    # The structural verifier above checks 0600, 0700, symlinks,
    # provenance, exact expected IDs, numeric ranges and classifications.
    try:
        with PRIVATE_OUTPUT.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        count = payload["analyses"]["210"]["exception_count"]
    except (OSError, ValueError, KeyError, TypeError):
        raise AuditBlocked("Cannot safely use existing private Achilles result") from None
    if type(count) is not int or not 0 <= count <= THRESHOLD:
        raise AuditBlocked("Unexpected private Achilles 210 result")
    return count


def query_overlap(connection, expected: int) -> dict[str, int]:
    verify_identity(connection, FROZEN_DATABASE)
    query = overlap_sql()
    if not query.lstrip().upper().startswith(";WITH DX_RANK AS"):
        raise AuditBlocked("Unexpected overlap query type")
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        row = cursor.fetchone()
        if row is None or len(row) != len(COLUMNS) or cursor.fetchone() is not None:
            raise AuditBlocked("Unexpected aggregate overlap result shape")
    finally:
        cursor.close()
    result = {}
    for name, value in zip(COLUMNS, row):
        if value is None or type(value) is bool:
            raise AuditBlocked("Missing aggregate overlap metric")
        try:
            n = int(value)
        except (ValueError, TypeError, OverflowError):
            raise AuditBlocked("Non-numeric overlap metric") from None
        if n != value or n < 0:
            raise AuditBlocked("Nonintegral or negative overlap metric")
        result[name] = n
    if (result["source_d0_patients"] != EXPECTED_SOURCE_D0
            or result["omop_lineage_d0_patients"] != EXPECTED_OMOP_D0):
        raise AuditBlocked("Locked Stage C/D D0 cohort anchors did not reproduce")
    if result["exception_visits"] != expected:
        raise AuditBlocked("Achilles 210 count mismatch; preserve evidence and stop")
    for name in COLUMNS[3:]:
        if result[name] > expected:
            raise AuditBlocked("Overlap count exceeds total Achilles exceptions")
    if (result["exception_index_visits_on_omop_d0"]
            > result["exception_visits_on_omop_d0_patients"]
            or result["exception_omop_d0_patients"]
            > result["exception_visits_on_omop_d0_patients"]
            or result["exception_acute_visits_30d_on_omop_d0"]
            > result["exception_acute_visits_90d_on_omop_d0"]):
        raise AuditBlocked("Overlap aggregate consistency checks failed")
    return result


def save_private_review(values: dict[str, int] | None, count: int) -> None:
    """Atomic create-only private JSON; never print small-cell results."""
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise AuditBlocked("Existing private overlap review must not be overwritten")
    if PRIVATE_DIR.is_symlink() or not PRIVATE_DIR.is_dir():
        raise AuditBlocked("Protected output directory missing or unsafe")
    if stat.S_IMODE(PRIVATE_DIR.stat().st_mode) != 0o700:
        raise AuditBlocked("Protected output directory must have mode 0700")
    if count == 0 and values is not None:
        raise AuditBlocked("Unexpected SQL results for zero-exception shortcut")
    if count > 0 and (values is None or set(values) != set(COLUMNS)):
        raise AuditBlocked("Incomplete overlap aggregate")
    payload = {
        "status": "private_postfreeze_achilles_210_cohort_overlap",
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
        "study_definition": "stage-c-stroke-d0-v1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "achilles_210_exception_count": count,
        "cohort_sql_performed": values is not None,
        "cohort_overlap": values,
        "caution": (
            "Patient/index and potential outcome-visit overlap only. "
            "Overlap does not establish downstream estimate change. "
            "Small counts remain local and are NOT disclosure-approved."
        ),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(OUTPUT, flags, 0o600)
    except OSError:
        raise AuditBlocked("Could not safely create new protected overlap file") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        OUTPUT.unlink(missing_ok=True)
        raise AuditBlocked("Could not complete private overlap output") from None


def run(config_path: str) -> None:
    verify_manifest()
    if OUTPUT.exists() or OUTPUT.is_symlink():
        raise AuditBlocked("Private overlap result already exists; refusing overwrite")
    existing_210 = validate_private_metadata()
    if existing_210 == 0:
        save_private_review(None, existing_210)
    else:
        password = os.getenv("OHDSI_QC_PASSWORD", "")
        if not password:
            raise AuditBlocked("OHDSI_QC_PASSWORD missing for restricted read-only SQL")
        config = verified_server_config(config_path)
        definition = ROOT / "study_definitions" / "stage_c_stroke_d0_v1.json"
        try:
            locked = json.loads(definition.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AuditBlocked("Missing or malformed locked Stage C D0 definition") from None
        if (locked.get("study_definition") != "stage-c-stroke-d0-v1"
                or locked.get("frozen_etl_sha")
                != "887e6f4d60a6b185e58b3c9fe8887472b49777e3"):
            raise AuditBlocked("Locked D0 definition or frozen ETL SHA mismatch")
        import pyodbc

        conn = pyodbc.connect(
            make_restricted_odbc_string(config, FROZEN_DATABASE, password),
            autocommit=True,
            timeout=20,
        )
        try:
            inspect_frozen(conn)
            values = query_overlap(conn, existing_210)
        finally:
            conn.close()
        save_private_review(values, existing_210)
    print("ACHILLES 210 LOCKED-COHORT OVERLAP AUDIT: COMPLETED")
    print("Private output: results/postfreeze_standard_qc/"
          "private_achilles_exception_counts/analysis_210_locked_d0_overlap.json")
    print("No patient rows retrieved; no sensitive counts printed.")
    print("Do NOT upload or paste the private output; no estimates were revised.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Protected SELECT-only review of Achilles 210 and locked D0"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    args = parser.parse_args()
    try:
        run(args.config)
    except AuditBlocked as exc:
        print("ACHILLES 210 OVERLAP AUDIT BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("ACHILLES 210 OVERLAP AUDIT BLOCKED: provider error suppressed",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
