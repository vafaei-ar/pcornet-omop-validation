from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy.engine import Connection

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists


ICD9_STROKE_CODES = {
    "43301",
    "43311",
    "43321",
    "43331",
    "43381",
    "43391",
    "43401",
    "43411",
    "43491",
}

# The phenotype supplement defines ICD-10 as H34.1 plus the I63 family.
ICD9_TYPES = {"09", "9", "ICD9", "ICD9CM"}
ICD10_TYPES = {"10", "ICD10", "ICD10CM"}
PRIMARY_PDX_VALUES = {"P"}
ELIGIBLE_ENC_TYPES = {"EI", "IP"}


@dataclass(frozen=True)
class CohortRow:
    patid: str
    index_date: date


def _sql_list(values: Iterable[str]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _norm(column: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {column})))), '.', '')"


def _norm_short(column: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {column}))))"


def _source_stroke_predicate(dx_column: str, type_column: str) -> str:
    dx = _norm(dx_column)
    dx_type = _norm_short(type_column)
    return f"""
    (
      ({dx_type} IN ({_sql_list(ICD9_TYPES)}) AND {dx} IN ({_sql_list(ICD9_STROKE_CODES)}))
      OR
      ({dx_type} IN ({_sql_list(ICD10_TYPES)}) AND ({dx} = 'H341' OR {dx} LIKE 'I63%'))
    )
    """.strip()


def _omop_stroke_predicate(source_value_column: str) -> str:
    value = _norm(source_value_column)
    return f"({value} IN ({_sql_list(ICD9_STROKE_CODES)}) OR {value} = 'H341' OR {value} LIKE 'I63%')"


def _exact_age_sql(birth_column: str, index_column: str) -> str:
    return f"""
    DATEDIFF(year, CAST({birth_column} AS date), CAST({index_column} AS date))
    - CASE
        WHEN DATEADD(
          year,
          DATEDIFF(year, CAST({birth_column} AS date), CAST({index_column} AS date)),
          CAST({birth_column} AS date)
        ) > CAST({index_column} AS date)
        THEN 1 ELSE 0
      END
    """.strip()


def _fetch_cohort(connection: Connection, sql: str) -> dict[str, CohortRow]:
    rows = connection.exec_driver_sql(sql).fetchall()
    result: dict[str, CohortRow] = {}
    for patid, index_date in rows:
        if isinstance(index_date, datetime):
            index_date = index_date.date()
        result[str(patid)] = CohortRow(str(patid), index_date)
    return result


def _jaccard(a: set[str], b: set[str]) -> float | None:
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def _date_agreement(a: dict[str, CohortRow], b: dict[str, CohortRow]) -> dict[str, int | float | None]:
    shared = sorted(set(a) & set(b))
    exact = 0
    within_one = 0
    for patid in shared:
        delta = abs((a[patid].index_date - b[patid].index_date).days)
        if delta == 0:
            exact += 1
        if delta <= 1:
            within_one += 1
    n = len(shared)
    return {
        "shared_patients": n,
        "exact_index_date_n": exact,
        "exact_index_date_fraction": exact / n if n else None,
        "within_1_day_n": within_one,
        "within_1_day_fraction": within_one / n if n else None,
    }


def _comparison(a: dict[str, CohortRow], b: dict[str, CohortRow]) -> dict[str, object]:
    a_ids = set(a)
    b_ids = set(b)
    return {
        "a_patients": len(a_ids),
        "b_patients": len(b_ids),
        "intersection": len(a_ids & b_ids),
        "a_only": len(a_ids - b_ids),
        "b_only": len(b_ids - a_ids),
        "union": len(a_ids | b_ids),
        "jaccard": _jaccard(a_ids, b_ids),
        **_date_agreement(a, b),
    }


def _source_sql(source_schema: str) -> str:
    stroke = _source_stroke_predicate("d.DX", "d.DX_TYPE")
    age = _exact_age_sql("dm.BIRTH_DATE", "e.ADMIT_DATE")
    return f"""
    WITH qualifying AS (
      SELECT DISTINCT
        CONVERT(nvarchar(255), e.PATID) AS patid,
        CONVERT(nvarchar(255), e.ENCOUNTERID) AS encounterid,
        CAST(e.ADMIT_DATE AS date) AS admit_date
      FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
      JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
        ON CONVERT(nvarchar(255), e.ENCOUNTERID) = CONVERT(nvarchar(255), d.ENCOUNTERID)
       AND CONVERT(nvarchar(255), e.PATID) = CONVERT(nvarchar(255), d.PATID)
      JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm
        ON CONVERT(nvarchar(255), dm.PATID) = CONVERT(nvarchar(255), e.PATID)
      WHERE {stroke}
        AND {_norm_short('d.PDX')} IN ({_sql_list(PRIMARY_PDX_VALUES)})
        AND {_norm_short('e.ENC_TYPE')} IN ({_sql_list(ELIGIBLE_ENC_TYPES)})
        AND e.ADMIT_DATE IS NOT NULL
        AND e.DISCHARGE_DATE IS NOT NULL
        AND CAST(e.DISCHARGE_DATE AS date) > CAST(e.ADMIT_DATE AS date)
        AND dm.BIRTH_DATE IS NOT NULL
        AND ({age}) >= 18
    ), ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY patid ORDER BY admit_date, encounterid) AS rn
      FROM qualifying
    )
    SELECT patid, admit_date AS index_date
    FROM ranked
    WHERE rn = 1
    ORDER BY patid
    """


def _source_gt24_count_sql(source_schema: str) -> str:
    stroke = _source_stroke_predicate("d.DX", "d.DX_TYPE")
    age = _exact_age_sql("dm.BIRTH_DATE", "e.ADMIT_DATE")
    return f"""
    WITH qualifying AS (
      SELECT DISTINCT
        CONVERT(nvarchar(255), e.PATID) AS patid,
        CONVERT(nvarchar(255), e.ENCOUNTERID) AS encounterid,
        DATEADD(second, CAST(e.ADMIT_TIME AS int), CAST(CAST(e.ADMIT_DATE AS date) AS datetime2)) AS admit_datetime,
        DATEADD(second, CAST(e.DISCHARGE_TIME AS int), CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2)) AS discharge_datetime
      FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
      JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
        ON CONVERT(nvarchar(255), e.ENCOUNTERID) = CONVERT(nvarchar(255), d.ENCOUNTERID)
       AND CONVERT(nvarchar(255), e.PATID) = CONVERT(nvarchar(255), d.PATID)
      JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm
        ON CONVERT(nvarchar(255), dm.PATID) = CONVERT(nvarchar(255), e.PATID)
      WHERE {stroke}
        AND {_norm_short('d.PDX')} IN ({_sql_list(PRIMARY_PDX_VALUES)})
        AND {_norm_short('e.ENC_TYPE')} IN ({_sql_list(ELIGIBLE_ENC_TYPES)})
        AND e.ADMIT_DATE IS NOT NULL
        AND e.DISCHARGE_DATE IS NOT NULL
        AND e.ADMIT_TIME IS NOT NULL
        AND e.DISCHARGE_TIME IS NOT NULL
        AND dm.BIRTH_DATE IS NOT NULL
        AND ({age}) >= 18
    )
    SELECT COUNT_BIG(DISTINCT patid)
    FROM qualifying
    WHERE discharge_datetime > DATEADD(hour, 24, admit_datetime)
    """


def _omop_standard_sql(target_schema: str) -> str:
    stroke = _omop_stroke_predicate("co.condition_source_value")
    age = _exact_age_sql("p.birth_datetime", "v.visit_start_date")
    return f"""
    WITH qualifying AS (
      SELECT DISTINCT
        CONVERT(nvarchar(255), p.person_source_value) AS patid,
        v.visit_occurrence_id,
        v.visit_start_date AS index_date
      FROM [{target_schema}].[condition_occurrence] co
      JOIN [{target_schema}].[person] p ON p.person_id = co.person_id
      JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id = co.visit_occurrence_id
      WHERE {stroke}
        AND {_norm_short('v.visit_source_value')} IN ({_sql_list(ELIGIBLE_ENC_TYPES)})
        AND v.visit_start_date IS NOT NULL
        AND v.visit_end_date IS NOT NULL
        AND v.visit_end_date > v.visit_start_date
        AND p.birth_datetime IS NOT NULL
        AND ({age}) >= 18
    ), ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY patid ORDER BY index_date, visit_occurrence_id) AS rn
      FROM qualifying
    )
    SELECT patid, index_date
    FROM ranked
    WHERE rn = 1
    ORDER BY patid
    """


def _omop_lineage_sql(source_schema: str, target_schema: str) -> str:
    stroke = _source_stroke_predicate("d.DX", "d.DX_TYPE")
    age = _exact_age_sql("p.birth_datetime", "v.visit_start_date")
    return f"""
    WITH qualifying AS (
      SELECT DISTINCT
        CONVERT(nvarchar(255), p.person_source_value) AS patid,
        v.visit_occurrence_id,
        v.visit_start_date AS index_date
      FROM [{target_schema}].[condition_occurrence] co
      JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
        ON x.condition_occurrence_id = co.condition_occurrence_id
       AND x.source_domain = 'DIAGNOSIS'
      JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d
        ON CONVERT(nvarchar(255), d.DIAGNOSISID) = x.source_record_id
      JOIN [{target_schema}].[person] p ON p.person_id = co.person_id
      JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id = co.visit_occurrence_id
      WHERE {stroke}
        AND {_norm_short('d.PDX')} IN ({_sql_list(PRIMARY_PDX_VALUES)})
        AND {_norm_short('v.visit_source_value')} IN ({_sql_list(ELIGIBLE_ENC_TYPES)})
        AND v.visit_start_date IS NOT NULL
        AND v.visit_end_date IS NOT NULL
        AND v.visit_end_date > v.visit_start_date
        AND p.birth_datetime IS NOT NULL
        AND ({age}) >= 18
    ), ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY patid ORDER BY index_date, visit_occurrence_id) AS rn
      FROM qualifying
    )
    SELECT patid, index_date
    FROM ranked
    WHERE rn = 1
    ORDER BY patid
    """


def _missing_dx_date_sql(source_schema: str) -> str:
    stroke = _source_stroke_predicate("d.DX", "d.DX_TYPE")
    return f"""
    SELECT COUNT_BIG(*)
    FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
    WHERE {stroke}
      AND {_norm_short('d.PDX')} IN ({_sql_list(PRIMARY_PDX_VALUES)})
      AND d.DX_DATE IS NULL
    """


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            required = [
                (source_schema, "PCORnet_DIAGNOSIS"),
                (source_schema, "PCORnet_ENCOUNTER"),
                (source_schema, "PCORnet_DEMOGRAPHIC"),
                (target_schema, "person"),
                (target_schema, "visit_occurrence"),
                (target_schema, "condition_occurrence"),
            ]
            for schema, table in required:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            source = _fetch_cohort(connection, _source_sql(source_schema))
            omop_standard = _fetch_cohort(connection, _omop_standard_sql(target_schema))

            lineage_available = table_exists(connection, source_schema, "etl_condition_occurrence_xwalk")
            omop_lineage = (
                _fetch_cohort(connection, _omop_lineage_sql(source_schema, target_schema))
                if lineage_available
                else {}
            )

            gt24_patients = int(connection.exec_driver_sql(_source_gt24_count_sql(source_schema)).scalar_one())
            missing_dx_date_rows = int(connection.exec_driver_sql(_missing_dx_date_sql(source_schema)).scalar_one())
    finally:
        engine.dispose()

    summary: dict[str, object] = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "phenotype": "D0 ischemic stroke",
        "primary_definition": {
            "diagnosis": "ICD-9 433.x1/434.x1 exact codes; ICD-10 H34.1 or I63 family",
            "dx_type_required": True,
            "primary_diagnosis": "PDX=P",
            "encounter_types": sorted(ELIGIBLE_ENC_TYPES),
            "minimum_age": 18,
            "length_of_stay_primary": "at least one overnight stay: discharge calendar date > admission calendar date",
            "indexing": "first qualifying admission per patient",
        },
        "sensitivity_definition": {
            "length_of_stay": ">24 hours using PCORnet ADMIT_TIME/DISCHARGE_TIME as seconds since midnight",
            "pcornet_patients": gt24_patients,
        },
        "cohorts": {
            "pcornet_primary": len(source),
            "omop_standard_without_pdx": len(omop_standard),
            "omop_lineage_with_pdx": len(omop_lineage) if lineage_available else None,
        },
        "representation_note": (
            "Standard OMOP fields do not preserve PCORnet PDX in the current validated ETL. "
            "The OMOP-standard cohort therefore omits the primary-diagnosis criterion. "
            "The lineage cohort reapplies PDX through the ETL crosswalk and staged PCORnet DIAGNOSIS table."
        ),
        "pcornet_vs_omop_standard": _comparison(source, omop_standard),
        "pcornet_vs_omop_lineage": _comparison(source, omop_lineage) if lineage_available else None,
        "diagnostics": {
            "lineage_available": lineage_available,
            "primary_stroke_diagnosis_rows_missing_dx_date": missing_dx_date_rows,
        },
    }

    out = Path(output_dir) if output_dir else Path("results/study_planning")
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "stroke_d0_reconciliation.json"
    csv_path = out / "stroke_d0_reconciliation_patients.csv"

    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")

    union_ids = sorted(set(source) | set(omop_standard) | set(omop_lineage))
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "patid",
                "pcornet_d0",
                "pcornet_index_date",
                "omop_standard_d0_without_pdx",
                "omop_standard_index_date",
                "omop_lineage_d0_with_pdx",
                "omop_lineage_index_date",
            ],
        )
        writer.writeheader()
        for patid in union_ids:
            writer.writerow(
                {
                    "patid": patid,
                    "pcornet_d0": int(patid in source),
                    "pcornet_index_date": source[patid].index_date if patid in source else "",
                    "omop_standard_d0_without_pdx": int(patid in omop_standard),
                    "omop_standard_index_date": omop_standard[patid].index_date if patid in omop_standard else "",
                    "omop_lineage_d0_with_pdx": int(patid in omop_lineage),
                    "omop_lineage_index_date": omop_lineage[patid].index_date if patid in omop_lineage else "",
                }
            )

    summary["output_json"] = str(json_path)
    summary["output_csv"] = str(csv_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile D0 ischemic stroke phenotype between PCORnet and validated OMOP")
    parser.add_argument("--config", default="config/etl.yaml", help="Path to local ETL YAML configuration")
    parser.add_argument("--output-dir", default=None, help="Optional output directory")
    args = parser.parse_args()

    summary = run(args.config, args.output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
