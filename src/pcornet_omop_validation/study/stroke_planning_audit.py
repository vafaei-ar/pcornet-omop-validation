from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists


REQUIRED_TABLES = (
    "PCORnet_ENCOUNTER",
    "PCORnet_DIAGNOSIS",
    "PCORnet_DEMOGRAPHIC",
)
OPTIONAL_TABLES = (
    "PCORnet_PROCEDURES",
    "PCORnet_LAB_RESULT_CM",
    "PCORnet_DEATH",
    "PCORnet_ENROLLMENT",
)


def _columns(connection, schema: str, table: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table
            """
        ),
        {"schema": schema, "table": table},
    ).fetchall()
    return {str(row[0]).upper(): str(row[0]) for row in rows}


def _pick(columns: dict[str, str], candidates: Iterable[str], *, required: bool = True) -> str | None:
    for candidate in candidates:
        found = columns.get(candidate.upper())
        if found:
            return found
    if required:
        raise RuntimeError(
            "None of the expected columns were found: " + ", ".join(candidates)
        )
    return None


def _q(name: str) -> str:
    return f"[{name.replace(']', ']]')}]"


def _stroke_code_sql(column: str) -> str:
    normalized = f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {column})))), '.', '')"
    return (
        f"({normalized} LIKE 'I63%' "
        f"OR {normalized} = 'H341' "
        f"OR {normalized} LIKE '433%1' "
        f"OR {normalized} LIKE '434%1')"
    )


def _scalar(connection, sql: str, params: dict | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(row._mapping) for row in rows]


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def run_stroke_planning_audit(config_path: str, top_codes: int = 40) -> int:
    """Profile the PCORnet source for stroke phenotypes and candidate outcomes.

    This stage is intentionally source-only. It does not compare PCORnet with OMOP and therefore
    cannot tune the ETL based on downstream representation differences. Its purpose is to determine
    whether the proposed stroke phenotypes and candidate prediction outcomes have adequate support.
    """

    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    schema = str(sql_cfg.get("source_schema", "dbo"))
    audit_path = Path(config.audit_dir).parent / "study_planning" / "stroke_source_planning_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            missing = [
                table for table in REQUIRED_TABLES if not table_exists(connection, schema, table)
            ]
            if missing:
                raise RuntimeError("Missing required source tables: " + ", ".join(missing))

            present_optional = {
                table: table_exists(connection, schema, table) for table in OPTIONAL_TABLES
            }

            enc_cols = _columns(connection, schema, "PCORnet_ENCOUNTER")
            dx_cols = _columns(connection, schema, "PCORnet_DIAGNOSIS")
            dem_cols = _columns(connection, schema, "PCORnet_DEMOGRAPHIC")

            enc_id = _pick(enc_cols, ["ENCOUNTERID"])
            enc_patid = _pick(enc_cols, ["PATID"])
            admit_date = _pick(enc_cols, ["ADMIT_DATE"])
            discharge_date = _pick(enc_cols, ["DISCHARGE_DATE"])
            enc_type = _pick(enc_cols, ["ENC_TYPE"], required=False)

            dx_enc = _pick(dx_cols, ["ENCOUNTERID"])
            dx_patid = _pick(dx_cols, ["PATID"])
            dx_code = _pick(dx_cols, ["DX"])

            dem_patid = _pick(dem_cols, ["PATID"])
            birth_date = _pick(dem_cols, ["BIRTH_DATE", "BIRTHDATE"])
            sex = _pick(dem_cols, ["SEX"], required=False)
            race = _pick(dem_cols, ["RACE"], required=False)
            hispanic = _pick(dem_cols, ["HISPANIC"], required=False)

            stroke_predicate = _stroke_code_sql(f"d.{_q(dx_code)}")
            enc_type_expr = (
                f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), e.{_q(enc_type)})))"
                if enc_type
                else "CAST(NULL AS nvarchar(20))"
            )
            age_expr = (
                f"DATEDIFF(year, TRY_CONVERT(date, dm.{_q(birth_date)}), TRY_CONVERT(date, e.{_q(admit_date)})) "
                f"- CASE WHEN DATEADD(year, DATEDIFF(year, TRY_CONVERT(date, dm.{_q(birth_date)}), "
                f"TRY_CONVERT(date, e.{_q(admit_date)})), TRY_CONVERT(date, dm.{_q(birth_date)})) "
                f"> TRY_CONVERT(date, e.{_q(admit_date)}) THEN 1 ELSE 0 END"
            )

            base_cte = f"""
            WITH stroke_dx AS (
              SELECT DISTINCT
                CONVERT(nvarchar(255), d.{_q(dx_enc)}) AS encounterid,
                CONVERT(nvarchar(255), d.{_q(dx_patid)}) AS patid
              FROM [{schema}].[PCORnet_DIAGNOSIS] d
              WHERE {stroke_predicate}
            ), candidate AS (
              SELECT
                CONVERT(nvarchar(255), e.{_q(enc_id)}) AS encounterid,
                CONVERT(nvarchar(255), e.{_q(enc_patid)}) AS patid,
                TRY_CONVERT(date, e.{_q(admit_date)}) AS admit_date,
                TRY_CONVERT(date, e.{_q(discharge_date)}) AS discharge_date,
                {enc_type_expr} AS enc_type,
                {age_expr} AS age_years,
                DATEDIFF(day, TRY_CONVERT(date, e.{_q(admit_date)}), TRY_CONVERT(date, e.{_q(discharge_date)})) AS los_calendar_days
              FROM [{schema}].[PCORnet_ENCOUNTER] e
              JOIN stroke_dx s
                ON s.encounterid=CONVERT(nvarchar(255), e.{_q(enc_id)})
               AND s.patid=CONVERT(nvarchar(255), e.{_q(enc_patid)})
              JOIN [{schema}].[PCORnet_DEMOGRAPHIC] dm
                ON CONVERT(nvarchar(255), dm.{_q(dem_patid)})=CONVERT(nvarchar(255), e.{_q(enc_patid)})
              WHERE TRY_CONVERT(date, e.{_q(admit_date)}) IS NOT NULL
                AND TRY_CONVERT(date, e.{_q(discharge_date)}) IS NOT NULL
            ), adult AS (
              SELECT * FROM candidate WHERE age_years >= 18
            ), planning_index_candidates AS (
              SELECT * FROM adult WHERE los_calendar_days >= 1
            ), first_index AS (
              SELECT *
              FROM (
                SELECT p.*, ROW_NUMBER() OVER (
                  PARTITION BY patid ORDER BY admit_date, encounterid
                ) AS rn
                FROM planning_index_candidates p
              ) x
              WHERE rn=1
            )
            """

            cohort_summary = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT
                      COUNT_BIG(*) AS adult_stroke_encounters,
                      COUNT_BIG(DISTINCT patid) AS adult_stroke_patients,
                      SUM(CASE WHEN los_calendar_days >= 1 THEN 1 ELSE 0 END) AS los_ge_1_calendar_day,
                      SUM(CASE WHEN los_calendar_days > 1 THEN 1 ELSE 0 END) AS los_gt_1_calendar_day,
                      MIN(admit_date) AS first_admit_date,
                      MAX(admit_date) AS last_admit_date
                    FROM adult
                    """
                )
            ).mappings().one()

            enc_type_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT COALESCE(enc_type, '(missing)') AS enc_type,
                           COUNT_BIG(*) AS encounters,
                           COUNT_BIG(DISTINCT patid) AS patients
                    FROM adult
                    GROUP BY enc_type
                    ORDER BY encounters DESC, enc_type
                    """
                )
            ).mappings().all()

            max_source_date = connection.execute(
                text(
                    f"SELECT MAX(COALESCE(TRY_CONVERT(date,{_q(discharge_date)}), TRY_CONVERT(date,{_q(admit_date)}))) "
                    f"FROM [{schema}].[PCORnet_ENCOUNTER]"
                )
            ).scalar_one()

            followup = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT
                      COUNT_BIG(*) AS index_patients,
                      SUM(CASE WHEN DATEADD(day,30,discharge_date) <= :max_source_date THEN 1 ELSE 0 END) AS calendar_eligible_30d,
                      SUM(CASE WHEN DATEADD(day,365,discharge_date) <= :max_source_date THEN 1 ELSE 0 END) AS calendar_eligible_365d
                    FROM first_index
                    """
                ),
                {"max_source_date": max_source_date},
            ).mappings().one()

            all_encounters_cte = f"""
            , future_encounters AS (
              SELECT
                i.patid,
                i.encounterid AS index_encounterid,
                i.discharge_date,
                CONVERT(nvarchar(255), e.{_q(enc_id)}) AS future_encounterid,
                TRY_CONVERT(date, e.{_q(admit_date)}) AS future_admit_date,
                {enc_type_expr.replace('e.', 'e.')} AS future_enc_type
              FROM first_index i
              JOIN [{schema}].[PCORnet_ENCOUNTER] e
                ON CONVERT(nvarchar(255), e.{_q(enc_patid)})=i.patid
               AND TRY_CONVERT(date, e.{_q(admit_date)}) > i.discharge_date
               AND TRY_CONVERT(date, e.{_q(admit_date)}) <= DATEADD(day,30,i.discharge_date)
            )
            """

            readmission = connection.execute(
                text(
                    base_cte
                    + all_encounters_cte
                    + """
                    SELECT
                      COUNT_BIG(DISTINCT CASE WHEN f.future_encounterid IS NOT NULL THEN i.patid END) AS any_30d_return,
                      COUNT_BIG(DISTINCT CASE WHEN f.future_enc_type IN ('IP','EI') THEN i.patid END) AS inpatient_or_ed_to_inpatient_30d
                    FROM first_index i
                    LEFT JOIN future_encounters f
                      ON f.patid=i.patid AND f.index_encounterid=i.encounterid
                    WHERE DATEADD(day,30,i.discharge_date) <= :max_source_date
                    """
                ),
                {"max_source_date": max_source_date},
            ).mappings().one()

            recurrent_cte = f"""
            , future_stroke AS (
              SELECT DISTINCT
                i.patid,
                i.encounterid AS index_encounterid,
                CONVERT(nvarchar(255), e.{_q(enc_id)}) AS future_encounterid,
                TRY_CONVERT(date, e.{_q(admit_date)}) AS future_admit_date
              FROM first_index i
              JOIN [{schema}].[PCORnet_ENCOUNTER] e
                ON CONVERT(nvarchar(255), e.{_q(enc_patid)})=i.patid
               AND TRY_CONVERT(date, e.{_q(admit_date)}) > i.discharge_date
               AND TRY_CONVERT(date, e.{_q(admit_date)}) <= DATEADD(day,365,i.discharge_date)
              JOIN [{schema}].[PCORnet_DIAGNOSIS] d
                ON CONVERT(nvarchar(255), d.{_q(dx_enc)})=CONVERT(nvarchar(255), e.{_q(enc_id)})
               AND CONVERT(nvarchar(255), d.{_q(dx_patid)})=CONVERT(nvarchar(255), e.{_q(enc_patid)})
              WHERE {_stroke_code_sql(f'd.{_q(dx_code)}')}
            )
            """

            recurrence = connection.execute(
                text(
                    base_cte
                    + recurrent_cte
                    + """
                    SELECT COUNT_BIG(DISTINCT CASE WHEN fs.future_encounterid IS NOT NULL THEN i.patid END) AS recurrent_stroke_365d
                    FROM first_index i
                    LEFT JOIN future_stroke fs
                      ON fs.patid=i.patid AND fs.index_encounterid=i.encounterid
                    WHERE DATEADD(day,365,i.discharge_date) <= :max_source_date
                    """
                ),
                {"max_source_date": max_source_date},
            ).mappings().one()

            subgroup_selects = []
            if sex:
                subgroup_selects.append(("sex", f"dm.{_q(sex)}"))
            if race:
                subgroup_selects.append(("race", f"dm.{_q(race)}"))
            if hispanic:
                subgroup_selects.append(("hispanic", f"dm.{_q(hispanic)}"))

            subgroups: dict[str, list[dict]] = {}
            for label, expr in subgroup_selects:
                rows = connection.execute(
                    text(
                        base_cte
                        + f"""
                        SELECT COALESCE(CONVERT(nvarchar(100), {expr}), '(missing)') AS category,
                               COUNT_BIG(*) AS patients
                        FROM first_index i
                        JOIN [{schema}].[PCORnet_DEMOGRAPHIC] dm
                          ON CONVERT(nvarchar(255), dm.{_q(dem_patid)})=i.patid
                        GROUP BY {expr}
                        ORDER BY patients DESC, category
                        """
                    )
                ).mappings().all()
                subgroups[label] = _rows_to_dicts(rows)

            age_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT
                      CASE
                        WHEN age_years < 45 THEN '18-44'
                        WHEN age_years < 65 THEN '45-64'
                        WHEN age_years < 75 THEN '65-74'
                        WHEN age_years < 85 THEN '75-84'
                        ELSE '85+'
                      END AS age_group,
                      COUNT_BIG(*) AS patients
                    FROM first_index
                    GROUP BY CASE
                        WHEN age_years < 45 THEN '18-44'
                        WHEN age_years < 65 THEN '45-64'
                        WHEN age_years < 75 THEN '65-74'
                        WHEN age_years < 85 THEN '75-84'
                        ELSE '85+'
                      END
                    ORDER BY MIN(age_years)
                    """
                )
            ).mappings().all()
            subgroups["age_group"] = _rows_to_dicts(age_rows)

            top_procedures: list[dict] = []
            if present_optional["PCORnet_PROCEDURES"]:
                px_cols = _columns(connection, schema, "PCORnet_PROCEDURES")
                px_patid = _pick(px_cols, ["PATID"])
                px_code = _pick(px_cols, ["PX"])
                px_type = _pick(px_cols, ["PX_TYPE"], required=False)
                px_date = _pick(px_cols, ["PX_DATE"])
                px_type_expr = (
                    f"CONVERT(nvarchar(50), p.{_q(px_type)})" if px_type else "CAST(NULL AS nvarchar(50))"
                )
                top_procedures = _rows_to_dicts(
                    connection.execute(
                        text(
                            base_cte
                            + f"""
                            SELECT TOP ({int(top_codes)})
                              {px_type_expr} AS px_type,
                              CONVERT(nvarchar(255), p.{_q(px_code)}) AS px,
                              COUNT_BIG(*) AS rows,
                              COUNT_BIG(DISTINCT i.patid) AS patients
                            FROM first_index i
                            JOIN [{schema}].[PCORnet_PROCEDURES] p
                              ON CONVERT(nvarchar(255), p.{_q(px_patid)})=i.patid
                             AND TRY_CONVERT(date, p.{_q(px_date)}) BETWEEN DATEADD(day,-2,i.admit_date) AND i.discharge_date
                            GROUP BY {px_type_expr}, CONVERT(nvarchar(255), p.{_q(px_code)})
                            ORDER BY rows DESC, px
                            """
                        )
                    ).mappings().all()
                )

            top_labs: list[dict] = []
            if present_optional["PCORnet_LAB_RESULT_CM"]:
                lab_cols = _columns(connection, schema, "PCORnet_LAB_RESULT_CM")
                lab_patid = _pick(lab_cols, ["PATID"])
                lab_loinc = _pick(lab_cols, ["LAB_LOINC", "LOINC"])
                lab_date = _pick(
                    lab_cols,
                    ["SPECIMEN_DATE", "RESULT_DATE", "LAB_ORDER_DATE"],
                    required=False,
                )
                date_predicate = (
                    f"AND TRY_CONVERT(date, l.{_q(lab_date)}) BETWEEN i.admit_date AND i.discharge_date"
                    if lab_date
                    else ""
                )
                top_labs = _rows_to_dicts(
                    connection.execute(
                        text(
                            base_cte
                            + f"""
                            SELECT TOP ({int(top_codes)})
                              CONVERT(nvarchar(255), l.{_q(lab_loinc)}) AS lab_loinc,
                              COUNT_BIG(*) AS rows,
                              COUNT_BIG(DISTINCT i.patid) AS patients
                            FROM first_index i
                            JOIN [{schema}].[PCORnet_LAB_RESULT_CM] l
                              ON CONVERT(nvarchar(255), l.{_q(lab_patid)})=i.patid
                              {date_predicate}
                            WHERE l.{_q(lab_loinc)} IS NOT NULL
                              AND LTRIM(RTRIM(CONVERT(nvarchar(255), l.{_q(lab_loinc)}))) <> ''
                            GROUP BY CONVERT(nvarchar(255), l.{_q(lab_loinc)})
                            ORDER BY rows DESC, lab_loinc
                            """
                        )
                    ).mappings().all()
                )

            mortality: dict | None = None
            if present_optional["PCORnet_DEATH"]:
                death_cols = _columns(connection, schema, "PCORnet_DEATH")
                death_patid = _pick(death_cols, ["PATID"])
                death_date = _pick(death_cols, ["DEATH_DATE"])
                mortality = dict(
                    connection.execute(
                        text(
                            base_cte
                            + f"""
                            SELECT
                              COUNT_BIG(DISTINCT CASE WHEN TRY_CONVERT(date,d.{_q(death_date)}) > i.discharge_date
                                                       AND TRY_CONVERT(date,d.{_q(death_date)}) <= DATEADD(day,30,i.discharge_date)
                                                     THEN i.patid END) AS death_30d,
                              COUNT_BIG(DISTINCT CASE WHEN TRY_CONVERT(date,d.{_q(death_date)}) > i.discharge_date
                                                       AND TRY_CONVERT(date,d.{_q(death_date)}) <= DATEADD(day,365,i.discharge_date)
                                                     THEN i.patid END) AS death_365d
                            FROM first_index i
                            LEFT JOIN [{schema}].[PCORnet_DEATH] d
                              ON CONVERT(nvarchar(255), d.{_q(death_patid)})=i.patid
                            """
                        )
                    ).mappings().one()
                )

            payload = {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "stroke_source_planning_audit",
                "purpose": (
                    "Source-only, representation-blinded planning audit for stroke phenotype reproducibility, "
                    "epidemiologic analyses, and prediction-outcome selection."
                ),
                "source_schema": schema,
                "source_max_encounter_date": max_source_date,
                "phenotype_anchor": {
                    "D0": "adult hospitalization with ischemic-stroke ICD code; manuscript used length of stay >24h",
                    "D1": "D0 plus CT or MRI plus lipid testing",
                    "D3": "D0 plus MRI plus lipid testing",
                    "stroke_codes": "ICD-9 433.x1/434.x1; ICD-10 I63 family/H34.1",
                    "note": (
                        "This audit does not hard-code D1/D3 CPT or LOINC lists because the uploaded manuscript "
                        "references supplementary code lists that were not present in the supplied document. "
                        "Top source procedure and LOINC codes around index admissions are reported for code-list verification."
                    ),
                },
                "length_of_stay_note": (
                    "PCORnet encounter dates are profiled at date granularity here. los_calendar_days>=1 is a provisional "
                    "planning proxy and is not claimed to be equivalent to the manuscript's >24-hour criterion. "
                    "Both >=1 and >1 calendar-day counts are reported so the final phenotype can be locked after source-field review."
                ),
                "cohort_summary": dict(cohort_summary),
                "encounter_type_distribution": _rows_to_dicts(enc_type_rows),
                "followup_calendar_availability": dict(followup),
                "candidate_outcomes": {
                    "30d_return": dict(readmission),
                    "365d_recurrent_ischaemic_stroke": dict(recurrence),
                    "mortality": mortality,
                    "definitions_are_provisional": True,
                },
                "subgroups": subgroups,
                "top_procedures_index_window_minus2_to_discharge": top_procedures,
                "top_loinc_index_admission": top_labs,
                "optional_tables_present": present_optional,
                "planning_rules": {
                    "index": "first provisional D0 encounter per patient",
                    "30d_readmission_candidate": "subsequent IP or EI encounter within 30 days after index discharge",
                    "365d_recurrence_candidate": "subsequent encounter carrying the same ischemic-stroke code family within 365 days",
                    "calendar_followup": "requires source encounter calendar to extend through the outcome window; enrollment continuity not yet imposed",
                    "comparison_with_omop": "none in this stage",
                },
            }
            _write_payload(audit_path, payload)

            print("Stroke source planning audit")
            print(f"  Adult stroke-coded encounters: {int(cohort_summary['adult_stroke_encounters']):,}")
            print(f"  Adult stroke-coded patients:   {int(cohort_summary['adult_stroke_patients']):,}")
            print(f"  LOS >=1 calendar day:          {int(cohort_summary['los_ge_1_calendar_day'] or 0):,}")
            print(f"  LOS >1 calendar day:           {int(cohort_summary['los_gt_1_calendar_day'] or 0):,}")
            print(f"  First/last stroke admission:   {cohort_summary['first_admit_date']} / {cohort_summary['last_admit_date']}")
            print(f"  Source max encounter date:     {max_source_date}")
            print("Candidate outcome counts among calendar-eligible index patients:")
            print(f"  30d any return:                {int(readmission['any_30d_return'] or 0):,}")
            print(f"  30d IP/EI readmission:         {int(readmission['inpatient_or_ed_to_inpatient_30d'] or 0):,}")
            print(f"  365d recurrent stroke:         {int(recurrence['recurrent_stroke_365d'] or 0):,}")
            if mortality is not None:
                print(f"  30d death:                     {int(mortality['death_30d'] or 0):,}")
                print(f"  365d death:                    {int(mortality['death_365d'] or 0):,}")
            print(f"Audit: {audit_path}")
            return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a source-only stroke study planning audit before PCORnet-vs-OMOP comparisons."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--top-codes", type=int, default=40)
    args = parser.parse_args(argv)
    if args.top_codes < 1 or args.top_codes > 500:
        parser.error("--top-codes must be between 1 and 500")
    return run_stroke_planning_audit(args.config, top_codes=args.top_codes)


if __name__ == "__main__":
    raise SystemExit(main())
