from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


PRIMARY_XWALK = "etl_condition_occurrence_xwalk"
OBSCLIN_XWALK = "etl_obs_clin_condition_xwalk"
PROCEDURE_XWALK = "etl_procedure_condition_xwalk"
ROUTE_TABLE = "etl_obs_clin_route"


def _safe_datetime_sql(date_expr: str, time_expr: str) -> str:
    return f"""
    CASE
      WHEN {date_expr} IS NULL THEN NULL
      WHEN TRY_CONVERT(float, {time_expr}) IS NULL
        OR TRY_CONVERT(float, {time_expr}) < 0
        OR TRY_CONVERT(float, {time_expr}) >= 86400
        THEN CAST(CAST({date_expr} AS date) AS datetime2(7))
      ELSE DATEADD(
        MILLISECOND,
        CAST(ROUND(TRY_CONVERT(float, {time_expr}) * 1000.0, 0) AS bigint),
        CAST(CAST({date_expr} AS date) AS datetime2(7))
      )
    END
    """


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def append_obs_clin_conditions(config: EtlConfig) -> dict[str, int | str]:
    """Append every OBS_CLIN route whose Standard target domain is Condition.

    OBS_CLIN lineage is deliberately separate from the primary DIAGNOSIS/
    CONDITION lineage table so route identifiers from independent route ledgers
    cannot collide. One-to-many OBS_CLIN-to-Condition mappings are supported.
    """
    engine = make_engine(config)
    audit_path = config.audit_dir / "condition_obs_clin_append.json"

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    try:
        with engine.begin() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "condition_occurrence"),
                (target_schema, PRIMARY_XWALK),
                (target_schema, ROUTE_TABLE),
                (target_schema, "person"),
                (target_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            route_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)} WHERE target_domain='Condition'",
            )
            route_distinct_source_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(DISTINCT source_obsclin_id) FROM {t(ROUTE_TABLE)} "
                "WHERE target_domain='Condition'",
            )
            source_rows_resolved = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(ROUTE_TABLE)} r
                JOIN {s('PCORnet_OBS_CLIN')} o
                  ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                WHERE r.target_domain='Condition'
                """,
            )
            if source_rows_resolved != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition route-to-source reconciliation failed: "
                    f"routes={route_rows:,}, resolved={source_rows_resolved:,}"
                )

            invalid_standard_target_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(ROUTE_TABLE)} r
                LEFT JOIN {t('concept')} c ON c.concept_id = r.target_concept_id
                WHERE r.target_domain='Condition'
                  AND COALESCE(r.target_concept_id, 0) <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Condition'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_standard_target_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition routes contain invalid nonzero Standard targets: "
                    f"{invalid_standard_target_rows:,}"
                )

            route_concept_zero_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)} "
                "WHERE target_domain='Condition' AND COALESCE(target_concept_id,0)=0",
            )
            unlinked_person_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(ROUTE_TABLE)} r
                JOIN {s('PCORnet_OBS_CLIN')} o
                  ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), o.PATID)))
                WHERE r.target_domain='Condition' AND p.person_id IS NULL
                """,
            )
            if unlinked_person_rows:
                raise RuntimeError(
                    f"OBS_CLIN Condition routes contain {unlinked_person_rows:,} unlinked persons"
                )

            current_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t('condition_occurrence')}")
            current_max = _scalar(
                con,
                f"SELECT COALESCE(MAX(condition_occurrence_id),0) FROM {t('condition_occurrence')}",
            )

            if table_exists(con, target_schema, OBSCLIN_XWALK):
                existing_xwalk = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(OBSCLIN_XWALK)}")
                mismatches = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    LEFT JOIN {t(ROUTE_TABLE)} r ON r.route_id = x.route_id
                    LEFT JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id = x.condition_occurrence_id
                    WHERE r.route_id IS NULL
                       OR r.target_domain <> 'Condition'
                       OR c.condition_occurrence_id IS NULL
                       OR c.condition_concept_id <> COALESCE(r.target_concept_id,0)
                       OR x.target_concept_id <> COALESCE(r.target_concept_id,0)
                    """,
                )
                if existing_xwalk != route_rows or mismatches:
                    raise RuntimeError(
                        "Existing OBS_CLIN Condition materialization does not match the route ledger"
                    )
                obsclin_target_rows = existing_xwalk
                obsclin_concept_zero = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id=x.condition_occurrence_id
                    WHERE c.condition_concept_id=0
                    """,
                )
                visit_linked = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id=x.condition_occurrence_id
                    WHERE c.visit_occurrence_id IS NOT NULL
                    """,
                )
                status = "already_matched"
                baseline_rows = current_rows - route_rows
                baseline_max = current_max - route_rows
                target_rows = current_rows
                target_max = current_max
            else:
                accounted_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(PRIMARY_XWALK)}")
                if table_exists(con, target_schema, PROCEDURE_XWALK):
                    accounted_rows += _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(PROCEDURE_XWALK)}")
                if accounted_rows != current_rows:
                    raise RuntimeError(
                        "Condition rows are not fully explained by existing route-aware lineage before "
                        f"OBS_CLIN append: condition={current_rows:,}, accounted={accounted_rows:,}"
                    )

                baseline_rows = current_rows
                baseline_max = current_max
                expected_target_rows = baseline_rows + route_rows
                expected_target_max = baseline_max + route_rows

                con.exec_driver_sql(
                    f"""
                    CREATE TABLE {t(OBSCLIN_XWALK)} (
                      route_id bigint NOT NULL,
                      source_record_id nvarchar(255) NOT NULL,
                      condition_occurrence_id bigint NOT NULL,
                      source_concept_id bigint NOT NULL,
                      target_concept_id bigint NOT NULL,
                      route_status varchar(64) NULL,
                      source_code_type nvarchar(50) NULL,
                      source_provenance nvarchar(50) NULL,
                      date_basis varchar(32) NOT NULL,
                      CONSTRAINT PK_{OBSCLIN_XWALK} PRIMARY KEY (route_id),
                      CONSTRAINT UQ_{OBSCLIN_XWALK}_condition UNIQUE (condition_occurrence_id)
                    )
                    """
                )

                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT
                            ROW_NUMBER() OVER (ORDER BY r.route_id) AS rn,
                            r.route_id,
                            r.source_obsclin_id,
                            COALESCE(r.source_concept_id,0) AS source_concept_id,
                            COALESCE(r.target_concept_id,0) AS target_concept_id,
                            r.route_status,
                            o.OBSCLIN_TYPE,
                            o.OBSCLIN_SOURCE
                          FROM {t(ROUTE_TABLE)} r
                          JOIN {s('PCORnet_OBS_CLIN')} o
                            ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                          WHERE r.target_domain='Condition'
                        )
                        INSERT INTO {t(OBSCLIN_XWALK)} (
                          route_id, source_record_id, condition_occurrence_id,
                          source_concept_id, target_concept_id, route_status,
                          source_code_type, source_provenance, date_basis
                        )
                        SELECT
                          route_id, source_obsclin_id, :base_id + CONVERT(bigint,rn),
                          source_concept_id, target_concept_id, route_status,
                          LEFT(CONVERT(nvarchar(50),OBSCLIN_TYPE),50),
                          LEFT(CONVERT(nvarchar(50),OBSCLIN_SOURCE),50),
                          'OBSCLIN_START_DATE'
                        FROM src
                        """
                    ),
                    {"base_id": baseline_max},
                )

                start_dt = _safe_datetime_sql("o.OBSCLIN_START_DATE", "o.OBSCLIN_START_TIME")
                stop_dt = _safe_datetime_sql("o.OBSCLIN_STOP_DATE", "o.OBSCLIN_STOP_TIME")
                con.execute(
                    text(
                        f"""
                        INSERT INTO {t('condition_occurrence')} (
                          condition_occurrence_id, person_id, condition_concept_id,
                          condition_start_date, condition_start_datetime,
                          condition_end_date, condition_end_datetime,
                          condition_type_concept_id, condition_status_concept_id,
                          stop_reason, provider_id, visit_occurrence_id, visit_detail_id,
                          condition_source_value, condition_source_concept_id,
                          condition_status_source_value
                        )
                        SELECT
                          x.condition_occurrence_id,
                          p.person_id,
                          x.target_concept_id,
                          CAST(o.OBSCLIN_START_DATE AS date),
                          {start_dt},
                          CAST(o.OBSCLIN_STOP_DATE AS date),
                          CASE WHEN o.OBSCLIN_STOP_DATE IS NULL THEN NULL ELSE {stop_dt} END,
                          0, 0, NULL, NULL, v.visit_occurrence_id, NULL,
                          LEFT(CONVERT(varchar(50),o.OBSCLIN_CODE),50),
                          x.source_concept_id,
                          NULL
                        FROM {t(OBSCLIN_XWALK)} x
                        JOIN {s('PCORnet_OBS_CLIN')} o
                          ON x.source_record_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                        JOIN {t('person')} p
                          ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), o.PATID)))
                        LEFT JOIN {t('etl_visit_occurrence_xwalk')} v
                          ON v.encounterid = LTRIM(RTRIM(CONVERT(nvarchar(255), o.ENCOUNTERID)))
                        """
                    )
                )

                target_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t('condition_occurrence')}")
                target_max = _scalar(
                    con,
                    f"SELECT COALESCE(MAX(condition_occurrence_id),0) FROM {t('condition_occurrence')}",
                )
                obsclin_target_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(OBSCLIN_XWALK)}")
                obsclin_concept_zero = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id=x.condition_occurrence_id
                    WHERE c.condition_concept_id=0
                    """,
                )
                mismatches = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    JOIN {t(ROUTE_TABLE)} r ON r.route_id=x.route_id
                    JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id=x.condition_occurrence_id
                    WHERE r.target_domain <> 'Condition'
                       OR c.condition_concept_id <> COALESCE(r.target_concept_id,0)
                       OR x.target_concept_id <> COALESCE(r.target_concept_id,0)
                    """,
                )
                visit_linked = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(OBSCLIN_XWALK)} x
                    JOIN {t('condition_occurrence')} c
                      ON c.condition_occurrence_id=x.condition_occurrence_id
                    WHERE c.visit_occurrence_id IS NOT NULL
                    """,
                )
                if target_rows != expected_target_rows or target_max != expected_target_max:
                    raise RuntimeError(
                        "OBS_CLIN Condition append row/ID reconciliation failed: "
                        f"target={target_rows:,}/{expected_target_rows:,}, "
                        f"max={target_max:,}/{expected_target_max:,}"
                    )
                if obsclin_target_rows != route_rows or mismatches:
                    raise RuntimeError(
                        "OBS_CLIN Condition route-aware lineage reconciliation failed"
                    )
                status = "matched"

            if obsclin_concept_zero != route_concept_zero_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition concept-zero reconciliation failed: "
                    f"target={obsclin_concept_zero:,}, routes={route_concept_zero_rows:,}"
                )

        payload = {
            "stage": "condition_obs_clin_append",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "status": status,
            "baseline_condition_rows": baseline_rows,
            "baseline_max_condition_occurrence_id": baseline_max,
            "obs_clin_condition_rows": route_rows,
            "route_distinct_source_rows": route_distinct_source_rows,
            "one_to_many_expansion_rows": route_rows - route_distinct_source_rows,
            "source_rows_resolved": source_rows_resolved,
            "target_rows": target_rows,
            "target_max_condition_occurrence_id": target_max,
            "concept_zero_rows": obsclin_concept_zero,
            "expected_concept_zero_rows": route_concept_zero_rows,
            "invalid_standard_target_rows": invalid_standard_target_rows,
            "unlinked_person_rows": unlinked_person_rows,
            "visit_linked_rows": visit_linked,
            "lineage_table": f"{target_schema}.{OBSCLIN_XWALK}",
            "policy": (
                "Materialize every OBS_CLIN Condition-domain route. Keep OBS_CLIN lineage in a "
                "route-aware ledger separate from primary DIAGNOSIS/CONDITION lineage; never "
                "select an arbitrary target when vocabulary routing is one-to-many."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
