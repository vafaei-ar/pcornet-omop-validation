from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


NUMERIC_TYPES = {
    "bigint", "decimal", "float", "int", "money", "numeric", "real",
    "smallint", "smallmoney", "tinyint",
}


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _column_type(con, schema: str, table: str, column: str) -> str:
    value = con.execute(
        text(
            """
            SELECT DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table AND COLUMN_NAME=:column
            """
        ),
        {"schema": schema, "table": table, "column": column},
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError(f"Column [{schema}].[{table}].[{column}] does not exist")
    return str(value).lower()


def _numeric_profile(con, table_ref: str, column: str) -> dict[str, object]:
    row = con.execute(
        text(
            f"""
            SELECT
              COUNT_BIG(*) AS total_rows,
              SUM(CASE WHEN [{column}] IS NOT NULL THEN 1 ELSE 0 END) AS nonnull_rows,
              MIN(TRY_CONVERT(float,[{column}])) AS min_value,
              MAX(TRY_CONVERT(float,[{column}])) AS max_value,
              SUM(CASE WHEN TRY_CONVERT(float,[{column}]) < 0
                         OR TRY_CONVERT(float,[{column}]) >= 86400 THEN 1 ELSE 0 END) AS outside_sas_seconds_rows,
              SUM(CASE WHEN TRY_CONVERT(float,[{column}]) > 2359 THEN 1 ELSE 0 END) AS above_hhmm_max_rows,
              SUM(CASE WHEN TRY_CONVERT(float,[{column}]) IS NOT NULL
                         AND ABS(TRY_CONVERT(float,[{column}]) - ROUND(TRY_CONVERT(float,[{column}]),0)) > 0.0000001
                       THEN 1 ELSE 0 END) AS fractional_rows,
              SUM(CASE WHEN TRY_CONVERT(float,[{column}]) BETWEEN 0 AND 2359
                         AND FLOOR(TRY_CONVERT(float,[{column}]) / 100) BETWEEN 0 AND 23
                         AND TRY_CONVERT(int, FLOOR(TRY_CONVERT(float,[{column}]))) % 100 BETWEEN 0 AND 59
                       THEN 1 ELSE 0 END) AS valid_hhmm_shape_rows,
              SUM(CASE WHEN TRY_CONVERT(float,[{column}]) BETWEEN 0 AND 2359
                         AND NOT (
                           FLOOR(TRY_CONVERT(float,[{column}]) / 100) BETWEEN 0 AND 23
                           AND TRY_CONVERT(int, FLOOR(TRY_CONVERT(float,[{column}]))) % 100 BETWEEN 0 AND 59
                         ) THEN 1 ELSE 0 END) AS invalid_hhmm_shape_rows
            FROM {table_ref}
            """
        )
    ).mappings().one()
    return {
        "total_rows": int(row["total_rows"] or 0),
        "nonnull_rows": int(row["nonnull_rows"] or 0),
        "min_value": None if row["min_value"] is None else float(row["min_value"]),
        "max_value": None if row["max_value"] is None else float(row["max_value"]),
        "outside_sas_seconds_rows": int(row["outside_sas_seconds_rows"] or 0),
        "above_hhmm_max_rows": int(row["above_hhmm_max_rows"] or 0),
        "fractional_rows": int(row["fractional_rows"] or 0),
        "valid_hhmm_shape_rows": int(row["valid_hhmm_shape_rows"] or 0),
        "invalid_hhmm_shape_rows": int(row["invalid_hhmm_shape_rows"] or 0),
    }


def _interpretation(data_type: str, profile: dict[str, object] | None) -> str:
    if data_type not in NUMERIC_TYPES:
        return "text_time_expected"
    assert profile is not None
    nonnull = int(profile["nonnull_rows"])
    if nonnull == 0:
        return "numeric_but_no_values"
    if int(profile["outside_sas_seconds_rows"]):
        return "invalid_numeric_time_values_present"
    if int(profile["above_hhmm_max_rows"]) or int(profile["invalid_hhmm_shape_rows"]):
        return "sas_seconds_supported"
    return "ambiguous_numeric_encoding"


def audit_visit_time_semantics(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "visit_time_semantics_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_ENCOUNTER"),
                (target_schema, "visit_occurrence"),
                (target_schema, "etl_visit_occurrence_xwalk"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            source_ref = f"[{source_schema}].[PCORnet_ENCOUNTER]"
            admit_type = _column_type(con, source_schema, "PCORnet_ENCOUNTER", "ADMIT_TIME")
            discharge_type = _column_type(con, source_schema, "PCORnet_ENCOUNTER", "DISCHARGE_TIME")

            admit_profile = _numeric_profile(con, source_ref, "ADMIT_TIME") if admit_type in NUMERIC_TYPES else None
            discharge_profile = _numeric_profile(con, source_ref, "DISCHARGE_TIME") if discharge_type in NUMERIC_TYPES else None

            interpretations = {
                "ADMIT_TIME": _interpretation(admit_type, admit_profile),
                "DISCHARGE_TIME": _interpretation(discharge_type, discharge_profile),
            }

            blockers: list[str] = []
            for column, interpretation in interpretations.items():
                if interpretation in {"ambiguous_numeric_encoding", "invalid_numeric_time_values_present"}:
                    blockers.append(f"{column}:{interpretation}")

            visit_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]")
            xwalk_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_visit_occurrence_xwalk]")
            if visit_rows != xwalk_rows:
                blockers.append(f"visit_lineage_mismatch:{visit_rows}!={xwalk_rows}")

            # Verify that materialized datetimes follow the declared source-type rule.
            # Numeric PCORnet/SAS times are seconds after midnight; text times use SQL time parsing.
            admit_expected = (
                "CASE WHEN e.ADMIT_DATE IS NULL THEN NULL "
                "WHEN e.ADMIT_TIME IS NULL THEN CAST(CAST(e.ADMIT_DATE AS date) AS datetime2(7)) "
                "WHEN TRY_CONVERT(float,e.ADMIT_TIME) < 0 OR TRY_CONVERT(float,e.ADMIT_TIME) >= 86400 "
                "THEN CAST(CAST(e.ADMIT_DATE AS date) AS datetime2(7)) "
                "ELSE DATEADD(MILLISECOND,CAST(ROUND(TRY_CONVERT(float,e.ADMIT_TIME)*1000.0,0) AS bigint),"
                "CAST(CAST(e.ADMIT_DATE AS date) AS datetime2(7))) END"
                if admit_type in NUMERIC_TYPES else
                "CASE WHEN e.ADMIT_DATE IS NULL THEN NULL "
                "WHEN TRY_CONVERT(time(7),NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100),e.ADMIT_TIME))),'')) IS NULL "
                "THEN CAST(CAST(e.ADMIT_DATE AS date) AS datetime2(7)) "
                "ELSE DATEADD(NANOSECOND,DATEDIFF_BIG(NANOSECOND,CAST('00:00:00' AS time(7)),"
                "TRY_CONVERT(time(7),NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100),e.ADMIT_TIME))),''))),"
                "CAST(CAST(e.ADMIT_DATE AS date) AS datetime2(7))) END"
            )
            discharge_expected = (
                "CASE WHEN e.DISCHARGE_DATE IS NULL THEN NULL "
                "WHEN e.DISCHARGE_TIME IS NULL THEN CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2(7)) "
                "WHEN TRY_CONVERT(float,e.DISCHARGE_TIME) < 0 OR TRY_CONVERT(float,e.DISCHARGE_TIME) >= 86400 "
                "THEN CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2(7)) "
                "ELSE DATEADD(MILLISECOND,CAST(ROUND(TRY_CONVERT(float,e.DISCHARGE_TIME)*1000.0,0) AS bigint),"
                "CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2(7))) END"
                if discharge_type in NUMERIC_TYPES else
                "CASE WHEN e.DISCHARGE_DATE IS NULL THEN NULL "
                "WHEN TRY_CONVERT(time(7),NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100),e.DISCHARGE_TIME))),'')) IS NULL "
                "THEN CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2(7)) "
                "ELSE DATEADD(NANOSECOND,DATEDIFF_BIG(NANOSECOND,CAST('00:00:00' AS time(7)),"
                "TRY_CONVERT(time(7),NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100),e.DISCHARGE_TIME))),''))),"
                "CAST(CAST(e.DISCHARGE_DATE AS date) AS datetime2(7))) END"
            )

            mismatch = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_visit_occurrence_xwalk] x
                JOIN {source_ref} e
                  ON LTRIM(RTRIM(CONVERT(nvarchar(255),e.ENCOUNTERID))) = x.encounterid
                JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id = x.visit_occurrence_id
                WHERE ISNULL(CONVERT(datetime2(7),v.visit_start_datetime),'19000101')
                        <> ISNULL(CONVERT(datetime2(7),{admit_expected}),'19000101')
                   OR ISNULL(CONVERT(datetime2(7),v.visit_end_datetime),'19000101')
                        <> ISNULL(CONVERT(datetime2(7),{discharge_expected}),'19000101')
                """,
            )
            if mismatch:
                blockers.append(f"materialized_datetime_mismatch_rows:{mismatch}")

        status = "matched" if not blockers else "blocked"
        payload = {
            "stage": "visit_time_semantics_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "admit_time_sql_type": admit_type,
            "discharge_time_sql_type": discharge_type,
            "admit_time_profile": admit_profile,
            "discharge_time_profile": discharge_profile,
            "interpretations": interpretations,
            "visit_rows": visit_rows,
            "visit_xwalk_rows": xwalk_rows,
            "materialized_datetime_mismatch_rows": mismatch,
            "hard_blockers": blockers,
            "status": status,
            "policy": (
                "PCORnet RDBMS encounter time is HH:MI text; when staging presents a numeric "
                "time column, interpret it as PCORnet/SAS seconds after midnight only when the "
                "observed numeric distribution is incompatible with pure HHMM encoding. Fail "
                "closed when numeric encoding remains ambiguous."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
