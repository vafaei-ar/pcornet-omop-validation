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
from pcornet_omop_validation.study.stroke_codes import (
    ELIGIBLE_ENC_TYPES,
    ICD10_STROKE_CODES,
    ICD10_TYPES,
    ICD9_STROKE_CODES,
    ICD9_TYPES,
    PRIMARY_PDX_VALUES,
)


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
      ({dx_type} IN ({_sql_list(ICD10_TYPES)}) AND {dx} IN ({_sql_list(ICD10_STROKE_CODES)}))
    )
    """.strip()


def _omop_stroke_predicate(source_value_column: str) -> str:
    value = _norm(source_value_column)
    all_codes = set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES)
    return f"{value} IN ({_sql_list(all_codes)})"


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


def _source_qualifying_cte(source_schema: str) -> str:
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
    """


def _source_sql(source_schema: str) -> str:
    return _source_qualifying_cte(source_schema) + """
    SELECT patid, admit_date AS index_date
    FROM ranked
    WHERE rn = 1
    ORDER BY patid
    """


def _source_index_sql(source_schema: str) -> str:
    return _source_qualifying_cte(source_schema) + """
    SELECT patid, encounterid, admit_date
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


def _fetch_cohort(connection: Connection, sql: str) -> dict[str, CohortRow]:
    result: dict[str, CohortRow] = {}
    for patid, index_date in connection.exec_driver_sql(sql).fetchall():
        if isinstance(index_date, datetime):
            index_date = index_date.date()
        result[str(patid)] = CohortRow(str(patid), index_date)
    return result


def _fetch_source_indexes(connection: Connection, source_schema: str) -> dict[str, tuple[str, date]]:
    result: dict[str, tuple[str, date]] = {}
    for patid, encounterid, admit_date in connection.exec_driver_sql(_source_index_sql(source_schema)).fetchall():
        if isinstance(admit_date, datetime):
            admit_date = admit_date.date()
        result[str(patid)] = (str(encounterid), admit_date)
    return result


def _jaccard(a: set[str], b: set[str]) -> float | None:
    union = a | b
    return len(a & b) / len(union) if union else None


def _date_agreement(a: dict[str, CohortRow], b: dict[str, CohortRow]) -> dict[str, int | float | None]:
    shared = sorted(set(a) & set(b))
    deltas = [abs((a[p].index_date - b[p].index_date).days) for p in shared]
    exact = sum(delta == 0 for delta in deltas)
    within_one = sum(delta <= 1 for delta in deltas)
    n = len(shared)
    return {
        "shared_patients": n,
        "exact_index_date_n": exact,
        "exact_index_date_fraction": exact / n if n else None,
        "within_1_day_n": within_one,
        "within_1_day_fraction": within_one / n if n else None,
    }


def _comparison(a: dict[str, CohortRow], b: dict[str, CohortRow]) -> dict[str, object]:
    a_ids, b_ids = set(a), set(b)
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


def _index_event_diagnostics(
    connection: Connection,
    source_schema: str,
    target_schema: str,
    source_indexes: dict[str, tuple[str, date]],
    omop_lineage: dict[str, CohortRow],
) -> list[dict[str, object]]:
    if not source_indexes:
        return []
    stroke = _source_stroke_predicate("d.DX", "d.DX_TYPE")
    rows: list[dict[str, object]] = []
    for patid, (encounterid, source_date) in source_indexes.items():
        diag = connection.exec_driver_sql(
            f"""
            SELECT
              COUNT_BIG(*) AS qualifying_dx_rows,
              SUM(CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END) AS missing_dx_date_rows,
              SUM(CASE WHEN x.condition_occurrence_id IS NOT NULL THEN 1 ELSE 0 END) AS diagnosis_lineage_rows,
              SUM(CASE WHEN co.condition_occurrence_id IS NOT NULL THEN 1 ELSE 0 END) AS condition_occurrence_rows
            FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
            LEFT JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
              ON x.source_domain='DIAGNOSIS'
             AND x.source_record_id=CONVERT(nvarchar(255), d.DIAGNOSISID)
            LEFT JOIN [{target_schema}].[condition_occurrence] co
              ON co.condition_occurrence_id=x.condition_occurrence_id
            WHERE CONVERT(nvarchar(255), d.PATID)=?
              AND CONVERT(nvarchar(255), d.ENCOUNTERID)=?
              AND {stroke}
              AND {_norm_short('d.PDX')} IN ({_sql_list(PRIMARY_PDX_VALUES)})
            """,
            (patid, encounterid),
        ).one()
        visit_xwalk = int(
            connection.exec_driver_sql(
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[etl_visit_occurrence_xwalk] WHERE encounterid=?",
                (encounterid,),
            ).scalar_one()
        )
        qdx = int(diag[0] or 0)
        missing = int(diag[1] or 0)
        lineage = int(diag[2] or 0)
        condition_rows = int(diag[3] or 0)
        omop_row = omop_lineage.get(patid)
        omop_date = omop_row.index_date if omop_row else None
        source_only = omop_row is None
        date_delta = (omop_date - source_date).days if omop_date else None

        if visit_xwalk == 0:
            category = "visit_not_transformed"
        elif qdx > 0 and missing == qdx:
            category = "all_index_stroke_dx_missing_dx_date"
        elif condition_rows == 0:
            category = "index_stroke_diagnosis_not_transformed"
        elif source_only:
            category = "other_source_only"
        elif date_delta != 0:
            category = "later_or_different_omop_index"
        else:
            category = "concordant"

        rows.append(
            {
                "patid": patid,
                "source_index_encounterid": encounterid,
                "source_index_date": source_date,
                "omop_lineage_index_date": omop_date,
                "index_date_delta_days": date_delta,
                "source_only": source_only,
                "qualifying_index_dx_rows": qdx,
                "missing_dx_date_rows": missing,
                "all_index_dx_missing_dx_date": bool(qdx and missing == qdx),
                "visit_xwalk_rows": visit_xwalk,
                "diagnosis_lineage_rows": lineage,
                "condition_occurrence_rows": condition_rows,
                "discordance_category": category,
            }
        )
    return rows


def _category_counts(rows: list[dict[str, object]], include_concordant: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        category = str(row["discordance_category"])
        if not include_concordant and category == "concordant":
            continue
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


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
                (source_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "person"),
                (target_schema, "visit_occurrence"),
                (target_schema, "condition_occurrence"),
            ]
            for schema, table in required:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            source = _fetch_cohort(connection, _source_sql(source_schema))
            source_indexes = _fetch_source_indexes(connection, source_schema)
            omop_standard = _fetch_cohort(connection, _omop_standard_sql(target_schema))
            lineage_available = table_exists(connection, source_schema, "etl_condition_occurrence_xwalk")
            omop_lineage = _fetch_cohort(connection, _omop_lineage_sql(source_schema, target_schema)) if lineage_available else {}
            gt24_patients = int(connection.exec_driver_sql(_source_gt24_count_sql(source_schema)).scalar_one())
            missing_dx_date_rows = int(connection.exec_driver_sql(_missing_dx_date_sql(source_schema)).scalar_one())
            diagnostic_rows = (
                _index_event_diagnostics(connection, source_schema, target_schema, source_indexes, omop_lineage)
                if lineage_available
                else []
            )
    finally:
        engine.dispose()

    focused = [
        row for row in diagnostic_rows
        if bool(row["source_only"]) or row["index_date_delta_days"] not in (None, 0)
    ]

    summary: dict[str, object] = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "phenotype": "D0 ischemic stroke",
        "primary_definition": {
            "diagnosis": f"exact prespecified codes: {len(ICD9_STROKE_CODES)} ICD-9 and {len(ICD10_STROKE_CODES)} ICD-10 normalized codes",
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
            "focused_discordant_patients": len(focused),
            "focused_discordance_categories": _category_counts(focused),
            "all_source_index_categories": _category_counts(diagnostic_rows, include_concordant=True),
        },
    }

    out = Path(output_dir) if output_dir else Path("results/study_planning")
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "stroke_d0_reconciliation.json"
    cohort_csv = out / "stroke_d0_reconciliation_patients.csv"
    discordance_csv = out / "stroke_d0_discordance_audit.csv"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")

    union_ids = sorted(set(source) | set(omop_standard) | set(omop_lineage))
    with cohort_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "patid", "pcornet_d0", "pcornet_index_date",
            "omop_standard_d0_without_pdx", "omop_standard_index_date",
            "omop_lineage_d0_with_pdx", "omop_lineage_index_date",
        ])
        writer.writeheader()
        for patid in union_ids:
            writer.writerow({
                "patid": patid,
                "pcornet_d0": int(patid in source),
                "pcornet_index_date": source[patid].index_date if patid in source else "",
                "omop_standard_d0_without_pdx": int(patid in omop_standard),
                "omop_standard_index_date": omop_standard[patid].index_date if patid in omop_standard else "",
                "omop_lineage_d0_with_pdx": int(patid in omop_lineage),
                "omop_lineage_index_date": omop_lineage[patid].index_date if patid in omop_lineage else "",
            })

    diagnostic_fields = [
        "patid", "source_index_encounterid", "source_index_date", "omop_lineage_index_date",
        "index_date_delta_days", "source_only", "qualifying_index_dx_rows", "missing_dx_date_rows",
        "all_index_dx_missing_dx_date", "visit_xwalk_rows", "diagnosis_lineage_rows",
        "condition_occurrence_rows", "discordance_category",
    ]
    with discordance_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=diagnostic_fields)
        writer.writeheader()
        writer.writerows(focused)

    summary["output_json"] = str(json_path)
    summary["output_csv"] = str(cohort_csv)
    summary["output_discordance_csv"] = str(discordance_csv)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile D0 ischemic stroke phenotype between PCORnet and validated OMOP")
    parser.add_argument("--config", default="config/etl.yaml", help="Path to local ETL YAML configuration")
    parser.add_argument("--output-dir", default=None, help="Optional output directory")
    args = parser.parse_args()
    print(json.dumps(run(args.config, args.output_dir), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
