from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


VISIT_CONCEPT_MAP = {
    "AV": 9202,
    "ED": 9203,
    "EI": 262,
    "IP": 9201,
    "IS": 42898160,
    "OS": 581385,
    "IC": 0,
    "TH": 0,
    "OA": 9202,
}

_NUMERIC_TYPES = {
    "bigint", "decimal", "float", "int", "money", "numeric",
    "real", "smallint", "smallmoney", "tinyint",
}


@dataclass(frozen=True)
class VisitOccurrenceTransformResult:
    source_rows: int
    eligible_rows: int
    excluded_rows: int
    excluded_missing_encounterid: int
    excluded_missing_patid: int
    excluded_unlinked_person: int
    excluded_missing_admit_date: int
    excluded_missing_discharge_date: int
    excluded_invalid_interval: int
    target_rows: int
    visit_concept_zero: int
    visit_source_concept_zero: int
    unknown_enc_type_rows: int
    crosswalk_rows: int
    status: str
    audit_path: Path


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_ENCOUNTER"),
        (target_schema, "person"),
        (target_schema, "visit_occurrence"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _validate_visit_concepts(connection, schema: str) -> None:
    ids = sorted({x for x in VISIT_CONCEPT_MAP.values() if x})
    values = ",".join(str(x) for x in ids)
    rows = connection.execute(text(f"""
        SELECT concept_id, domain_id, standard_concept, invalid_reason
        FROM [{schema}].[concept]
        WHERE concept_id IN ({values})
    """)).fetchall()
    found = {int(r[0]) for r in rows}
    missing = sorted(set(ids) - found)
    invalid = [
        r for r in rows
        if r[1] != "Visit" or r[2] != "S" or r[3] is not None
    ]
    if missing or invalid:
        raise RuntimeError(
            "Configured visit concepts are not active Standard Visit concepts: "
            f"missing={missing}; invalid={invalid}"
        )


def _column_data_type(connection, schema: str, table: str, column: str) -> str:
    value = connection.execute(
        text("""
            SELECT DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = :schema
              AND TABLE_NAME = :table
              AND COLUMN_NAME = :column
        """),
        {"schema": schema, "table": table, "column": column},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"Missing required column [{schema}].[{table}].[{column}]")
    return str(value).lower()


def _case_sql(column: str) -> str:
    clauses = " ".join(
        f"WHEN {column} = '{key}' THEN {value}"
        for key, value in VISIT_CONCEPT_MAP.items()
    )
    return f"CASE {clauses} ELSE 0 END"


def _datetime_sql(date_column: str, time_column: str, time_type: str) -> str:
    """Build an OMOP datetime from PCORnet date/time without inventing time values.

    PCORnet defines RDBMS time as HH:MI text and SAS time as numeric seconds
    after midnight. Parquet/SAS staging can therefore legitimately produce a
    SQL numeric column. Numeric values are interpreted only as SAS-style
    seconds after midnight. Invalid/nonrepresentable values fall back to the
    source date at midnight while the source date itself remains unchanged.
    """
    if time_type in _NUMERIC_TYPES:
        return f"""
        CASE
          WHEN {date_column} IS NULL THEN NULL
          WHEN {time_column} IS NULL THEN CAST({date_column} AS datetime2(7))
          WHEN TRY_CONVERT(float, {time_column}) >= 0
           AND TRY_CONVERT(float, {time_column}) < 86400
            THEN DATEADD(
              millisecond,
              CONVERT(bigint, ROUND(TRY_CONVERT(float, {time_column}) * 1000.0, 0)),
              CAST(CAST({date_column} AS date) AS datetime2(7))
            )
          ELSE CAST({date_column} AS datetime2(7))
        END
        """.strip()

    time_text = (
        f"NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(64), {time_column}))), '')"
    )
    return f"""
    CASE
      WHEN {date_column} IS NULL THEN NULL
      WHEN TRY_CONVERT(time(7), {time_text}) IS NULL
        THEN CAST({date_column} AS datetime2(7))
      ELSE TRY_CONVERT(
        datetime2(7),
        CONVERT(char(10), CAST({date_column} AS date), 23) + ' ' +
        CONVERT(varchar(30), TRY_CONVERT(time(7), {time_text}))
      )
    END
    """.strip()


def _invalid_numeric_time_rows(
    connection, schema: str, column: str, data_type: str
) -> int:
    if data_type not in _NUMERIC_TYPES:
        return 0
    return _scalar(connection, f"""
        SELECT COUNT_BIG(*)
        FROM [{schema}].[PCORnet_ENCOUNTER]
        WHERE {column} IS NOT NULL
          AND (
               TRY_CONVERT(float, {column}) < 0
            OR TRY_CONVERT(float, {column}) >= 86400
            OR TRY_CONVERT(float, {column}) IS NULL
          )
    """)


def transform_visit_occurrence(config: EtlConfig) -> VisitOccurrenceTransformResult:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError("Visit ETL requires missing_required_date=exclude")
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError("Visit ETL requires unmapped_standard_concept=concept_zero")

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "visit_occurrence_transform.json"
    xwalk_table = "etl_visit_occurrence_xwalk"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            _validate_visit_concepts(connection, target_schema)

            admit_time_type = _column_data_type(
                connection, source_schema, "PCORnet_ENCOUNTER", "ADMIT_TIME"
            )
            discharge_time_type = _column_data_type(
                connection, source_schema, "PCORnet_ENCOUNTER", "DISCHARGE_TIME"
            )
            invalid_admit_time_rows = _invalid_numeric_time_rows(
                connection, source_schema, "ADMIT_TIME", admit_time_type
            )
            invalid_discharge_time_rows = _invalid_numeric_time_rows(
                connection, source_schema, "DISCHARGE_TIME", discharge_time_type
            )

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_ENCOUNTER]",
            )
            duplicate_encounterids = _scalar(connection, f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT ENCOUNTERID
                    FROM [{source_schema}].[PCORnet_ENCOUNTER]
                    WHERE ENCOUNTERID IS NOT NULL
                      AND LTRIM(RTRIM(CONVERT(nvarchar(255), ENCOUNTERID))) <> ''
                    GROUP BY ENCOUNTERID
                    HAVING COUNT_BIG(*) > 1
                ) d
            """)
            if duplicate_encounterids:
                raise RuntimeError(
                    f"PCORnet_ENCOUNTER has {duplicate_encounterids:,} duplicate ENCOUNTERID groups"
                )

            classification_cte = f"""
            WITH classified AS (
              SELECT e.*, p.person_id,
                     CASE
                       WHEN e.ENCOUNTERID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), e.ENCOUNTERID))) = ''
                         THEN 'missing_encounterid'
                       WHEN e.PATID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), e.PATID))) = ''
                         THEN 'missing_patid'
                       WHEN p.person_id IS NULL THEN 'unlinked_person'
                       WHEN e.ADMIT_DATE IS NULL THEN 'missing_admit_date'
                       WHEN e.DISCHARGE_DATE IS NULL THEN 'missing_discharge_date'
                       WHEN CAST(e.DISCHARGE_DATE AS date) < CAST(e.ADMIT_DATE AS date)
                         THEN 'invalid_interval'
                       ELSE 'eligible'
                     END AS record_status
              FROM [{source_schema}].[PCORnet_ENCOUNTER] e
              LEFT JOIN [{target_schema}].[person] p
                ON CONVERT(nvarchar(50), e.PATID) = p.person_source_value
            )
            """
            counts = dict(connection.execute(text(
                classification_cte
                + " SELECT record_status, COUNT_BIG(*) FROM classified GROUP BY record_status"
            )).fetchall())

            eligible_rows = int(counts.get("eligible", 0))
            excluded_missing_encounterid = int(counts.get("missing_encounterid", 0))
            excluded_missing_patid = int(counts.get("missing_patid", 0))
            excluded_unlinked_person = int(counts.get("unlinked_person", 0))
            excluded_missing_admit_date = int(counts.get("missing_admit_date", 0))
            excluded_missing_discharge_date = int(counts.get("missing_discharge_date", 0))
            excluded_invalid_interval = int(counts.get("invalid_interval", 0))
            excluded_rows = source_rows - eligible_rows

            known = ",".join(f"'{x}'" for x in VISIT_CONCEPT_MAP)
            unknown_enc_type_rows = _scalar(connection, f"""
                {classification_cte}
                SELECT COUNT_BIG(*) FROM classified
                WHERE record_status='eligible'
                  AND (ENC_TYPE IS NULL OR UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), ENC_TYPE)))) NOT IN ({known}))
            """)

            existing = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]",
            )
            if existing:
                if existing != eligible_rows:
                    raise RuntimeError(
                        f"visit_occurrence has {existing:,} rows; expected {eligible_rows:,}"
                    )
                status = "already_loaded_matched"
            else:
                visit_case = _case_sql(
                    "UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), ENC_TYPE))))"
                )
                start_dt = _datetime_sql("ADMIT_DATE", "ADMIT_TIME", admit_time_type)
                end_dt = _datetime_sql(
                    "DISCHARGE_DATE", "DISCHARGE_TIME", discharge_time_type
                )
                connection.exec_driver_sql(f"""
                    {classification_cte},
                    eligible AS (
                      SELECT ROW_NUMBER() OVER (
                               ORDER BY CONVERT(nvarchar(255), ENCOUNTERID)
                             ) AS visit_occurrence_id,
                             *
                      FROM classified
                      WHERE record_status='eligible'
                    )
                    INSERT INTO [{target_schema}].[visit_occurrence] (
                      visit_occurrence_id, person_id, visit_concept_id,
                      visit_start_date, visit_start_datetime,
                      visit_end_date, visit_end_datetime,
                      visit_type_concept_id, provider_id, care_site_id,
                      visit_source_value, visit_source_concept_id,
                      admitted_from_concept_id, admitted_from_source_value,
                      discharged_to_concept_id, discharged_to_source_value
                    )
                    SELECT
                      visit_occurrence_id, person_id, {visit_case},
                      CAST(ADMIT_DATE AS date), {start_dt},
                      CAST(DISCHARGE_DATE AS date), {end_dt},
                      0, NULL, NULL,
                      CONVERT(nvarchar(50), ENC_TYPE), 0,
                      0, CONVERT(nvarchar(50), ADMITTING_SOURCE),
                      0, CONVERT(nvarchar(50), DISCHARGE_DISPOSITION)
                    FROM eligible
                """)
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]",
            )
            if target_rows != eligible_rows:
                raise RuntimeError(
                    f"Visit reconciliation failed: eligible={eligible_rows:,}, target={target_rows:,}"
                )

            if table_exists(connection, target_schema, xwalk_table):
                crosswalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{xwalk_table}]",
                )
            else:
                connection.exec_driver_sql(f"""
                    CREATE TABLE [{target_schema}].[{xwalk_table}] (
                      encounterid nvarchar(255) NOT NULL PRIMARY KEY,
                      visit_occurrence_id bigint NOT NULL UNIQUE
                    )
                """)
                connection.exec_driver_sql(f"""
                    {classification_cte},
                    eligible AS (
                      SELECT ROW_NUMBER() OVER (
                               ORDER BY CONVERT(nvarchar(255), ENCOUNTERID)
                             ) AS visit_occurrence_id,
                             ENCOUNTERID
                      FROM classified
                      WHERE record_status='eligible'
                    )
                    INSERT INTO [{target_schema}].[{xwalk_table}]
                      (encounterid, visit_occurrence_id)
                    SELECT CONVERT(nvarchar(255), ENCOUNTERID), visit_occurrence_id
                    FROM eligible
                """)
                connection.commit()
                crosswalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{xwalk_table}]",
                )

            if crosswalk_rows != target_rows:
                raise RuntimeError(
                    f"Visit xwalk reconciliation failed: xwalk={crosswalk_rows:,}, target={target_rows:,}"
                )

            visit_concept_zero = _scalar(connection, f"""
                SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]
                WHERE visit_concept_id=0
            """)
            visit_source_concept_zero = _scalar(connection, f"""
                SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]
                WHERE visit_source_concept_id=0
            """)
    finally:
        engine.dispose()

    result = VisitOccurrenceTransformResult(
        source_rows=source_rows,
        eligible_rows=eligible_rows,
        excluded_rows=excluded_rows,
        excluded_missing_encounterid=excluded_missing_encounterid,
        excluded_missing_patid=excluded_missing_patid,
        excluded_unlinked_person=excluded_unlinked_person,
        excluded_missing_admit_date=excluded_missing_admit_date,
        excluded_missing_discharge_date=excluded_missing_discharge_date,
        excluded_invalid_interval=excluded_invalid_interval,
        target_rows=target_rows,
        visit_concept_zero=visit_concept_zero,
        visit_source_concept_zero=visit_source_concept_zero,
        unknown_enc_type_rows=unknown_enc_type_rows,
        crosswalk_rows=crosswalk_rows,
        status=status,
        audit_path=audit_path,
    )

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "visit_occurrence",
        "source_table": f"{source_schema}.PCORnet_ENCOUNTER",
        "target_table": f"{target_schema}.visit_occurrence",
        "lineage_table": f"{target_schema}.{xwalk_table}",
        "time_semantics": {
            "pcornet_rule": "RDBMS time is HH:MI text; SAS numeric time is seconds after midnight",
            "admit_time_sql_type": admit_time_type,
            "discharge_time_sql_type": discharge_time_type,
            "invalid_numeric_admit_time_rows_fallback_midnight": invalid_admit_time_rows,
            "invalid_numeric_discharge_time_rows_fallback_midnight": invalid_discharge_time_rows,
        },
        "mapping_strategy": {
            "visit_concept": (
                "broad active Standard OMOP Visit concept when source semantics justify it; "
                "IC, TH, ambiguous/unsupported values -> 0 with exact ENC_TYPE preserved"
            ),
            "visit_source_concept": "0; exact PCORnet ENC_TYPE preserved in visit_source_value",
            "visit_type_concept": "0 because ENCOUNTER does not establish a specific OMOP provenance type",
            "missing_required_date": "exclude",
        },
        "result": {**asdict(result), "audit_path": str(audit_path)},
    }, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return result
