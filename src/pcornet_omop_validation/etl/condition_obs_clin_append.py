from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


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
        CAST(
          ROUND(
            TRY_CONVERT(float, {time_expr}) * 1000.0,
            0
          ) AS bigint
        ),
        CAST(CAST({date_expr} AS date) AS datetime2(7))
      )
    END
    """


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def append_obs_clin_conditions(
    config: EtlConfig,
) -> dict[str, int | str]:
    """Append OBS_CLIN records routed to the OMOP Condition domain.

    Reconciliation is source- and route-derived. No site-specific row counts or
    target concept IDs are embedded in the transformation. Nonzero target
    concepts must be active Standard Condition concepts; concept_id=0 is
    retained when the route ledger explicitly carries an unresolved Condition
    route.
    """
    engine = make_engine(config)
    audit_path = config.audit_dir / "condition_obs_clin_append.json"

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    source_obs = f"[{source_schema}].[PCORnet_OBS_CLIN]"
    condition = f"[{target_schema}].[condition_occurrence]"
    xwalk = f"[{target_schema}].[etl_condition_occurrence_xwalk]"
    routes = f"[{target_schema}].[etl_obs_clin_route]"
    person = f"[{target_schema}].[person]"
    visit_xwalk = f"[{target_schema}].[etl_visit_occurrence_xwalk]"
    concept = f"[{target_schema}].[concept]"

    try:
        with engine.begin() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "condition_occurrence"),
                (target_schema, "etl_condition_occurrence_xwalk"),
                (target_schema, "etl_obs_clin_route"),
                (target_schema, "person"),
                (target_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            route_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM {routes} "
                        "WHERE target_domain = 'Condition'"
                    )
                ).scalar_one()
            )
            route_distinct_source_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(DISTINCT source_obsclin_id) "
                        f"FROM {routes} WHERE target_domain = 'Condition'"
                    )
                ).scalar_one()
            )
            if route_distinct_source_rows != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition routing is not one-to-one by source "
                    f"record: routes={route_rows:,}, distinct_sources="
                    f"{route_distinct_source_rows:,}"
                )

            source_rows_resolved = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {routes} r
                        JOIN {source_obs} o
                          ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                               nvarchar(255), o.OBSCLINID
                             )))
                        WHERE r.target_domain = 'Condition'
                        """
                    )
                ).scalar_one()
            )
            if source_rows_resolved != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition route-to-source reconciliation failed: "
                    f"routes={route_rows:,}, resolved={source_rows_resolved:,}"
                )

            invalid_standard_target_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {routes} r
                        LEFT JOIN {concept} c
                          ON c.concept_id = r.target_concept_id
                        WHERE r.target_domain = 'Condition'
                          AND COALESCE(r.target_concept_id, 0) <> 0
                          AND (
                               c.concept_id IS NULL
                            OR c.domain_id <> 'Condition'
                            OR c.standard_concept <> 'S'
                            OR c.invalid_reason IS NOT NULL
                          )
                        """
                    )
                ).scalar_one()
            )
            if invalid_standard_target_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition routes contain nonzero target concepts "
                    "that are not active Standard Condition concepts: "
                    f"{invalid_standard_target_rows:,}"
                )

            route_concept_zero_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM {routes} "
                        "WHERE target_domain = 'Condition' "
                        "AND COALESCE(target_concept_id, 0) = 0"
                    )
                ).scalar_one()
            )

            current_rows = int(
                con.execute(text(f"SELECT COUNT_BIG(*) FROM {condition}")).scalar_one()
            )
            current_max = int(
                con.execute(
                    text(
                        f"SELECT COALESCE(MAX(condition_occurrence_id), 0) "
                        f"FROM {condition}"
                    )
                ).scalar_one()
            )
            xwalk_rows = int(
                con.execute(text(f"SELECT COUNT_BIG(*) FROM {xwalk}")).scalar_one()
            )
            existing_obsclin_xwalk = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM {xwalk} "
                        "WHERE source_domain = 'OBS_CLIN'"
                    )
                ).scalar_one()
            )

            # Idempotent recognition of an already materialized append.
            if existing_obsclin_xwalk:
                obsclin_target_rows = int(
                    con.execute(
                        text(
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM {condition} c
                            JOIN {xwalk} x
                              ON x.condition_occurrence_id = c.condition_occurrence_id
                            WHERE x.source_domain = 'OBS_CLIN'
                            """
                        )
                    ).scalar_one()
                )
                route_target_mismatch_rows = int(
                    con.execute(
                        text(
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM {routes} r
                            JOIN {xwalk} x
                              ON x.source_domain = 'OBS_CLIN'
                             AND x.source_record_id = r.source_obsclin_id
                            JOIN {condition} c
                              ON c.condition_occurrence_id = x.condition_occurrence_id
                            WHERE r.target_domain = 'Condition'
                              AND c.condition_concept_id <>
                                  COALESCE(r.target_concept_id, 0)
                            """
                        )
                    ).scalar_one()
                )
                if (
                    existing_obsclin_xwalk != route_rows
                    or obsclin_target_rows != route_rows
                    or route_target_mismatch_rows != 0
                ):
                    raise RuntimeError(
                        "Existing OBS_CLIN Condition materialization does not "
                        "match the current route ledger"
                    )
                return {
                    "status": "already_matched",
                    "baseline_condition_rows": current_rows - route_rows,
                    "obs_clin_condition_rows": route_rows,
                    "target_rows": current_rows,
                    "target_max_condition_occurrence_id": current_max,
                    "concept_zero_rows": route_concept_zero_rows,
                    "audit_path": str(audit_path),
                }

            if xwalk_rows != current_rows:
                raise RuntimeError(
                    "Condition target and lineage row counts differ before "
                    f"OBS_CLIN append: condition={current_rows:,}, "
                    f"xwalk={xwalk_rows:,}"
                )

            baseline_rows = current_rows
            baseline_max = current_max
            expected_target_rows = baseline_rows + route_rows
            expected_target_max = baseline_max + route_rows

            condition_datetime = _safe_datetime_sql(
                "o.OBSCLIN_START_DATE",
                "o.OBSCLIN_START_TIME",
            )

            con.execute(
                text(
                    f"""
                    INSERT INTO {xwalk} (
                        source_domain,
                        source_record_id,
                        condition_occurrence_id,
                        source_code_type,
                        source_provenance,
                        date_basis
                    )
                    SELECT
                        'OBS_CLIN',
                        r.source_obsclin_id,
                        {baseline_max}
                        + ROW_NUMBER() OVER (
                            ORDER BY r.source_obsclin_id
                          ),
                        LEFT(CONVERT(nvarchar(50), o.OBSCLIN_TYPE), 50),
                        LEFT(CONVERT(nvarchar(50), o.OBSCLIN_SOURCE), 50),
                        'OBSCLIN_START_DATE'
                    FROM {routes} r
                    JOIN {source_obs} o
                      ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    WHERE r.target_domain = 'Condition'
                    """
                )
            )

            new_xwalk_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM {xwalk} "
                        "WHERE source_domain = 'OBS_CLIN'"
                    )
                ).scalar_one()
            )
            if new_xwalk_rows != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition xwalk reconciliation failed: "
                    f"xwalk={new_xwalk_rows:,}, routes={route_rows:,}"
                )

            con.execute(
                text(
                    f"""
                    INSERT INTO {condition} (
                        condition_occurrence_id,
                        person_id,
                        condition_concept_id,
                        condition_start_date,
                        condition_start_datetime,
                        condition_end_date,
                        condition_end_datetime,
                        condition_type_concept_id,
                        condition_status_concept_id,
                        stop_reason,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        condition_source_value,
                        condition_source_concept_id,
                        condition_status_source_value
                    )
                    SELECT
                        x.condition_occurrence_id,
                        p.person_id,
                        COALESCE(CONVERT(int, r.target_concept_id), 0),
                        CAST(o.OBSCLIN_START_DATE AS date),
                        {condition_datetime},
                        CAST(o.OBSCLIN_STOP_DATE AS date),
                        CASE
                            WHEN o.OBSCLIN_STOP_DATE IS NULL THEN NULL
                            ELSE {_safe_datetime_sql(
                                "o.OBSCLIN_STOP_DATE",
                                "o.OBSCLIN_STOP_TIME",
                            )}
                        END,
                        0,
                        0,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), o.OBSCLIN_CODE), 50),
                        COALESCE(CONVERT(int, r.source_concept_id), 0),
                        NULL
                    FROM {routes} r
                    JOIN {source_obs} o
                      ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    JOIN {xwalk} x
                      ON x.source_domain = 'OBS_CLIN'
                     AND x.source_record_id = r.source_obsclin_id
                    JOIN {person} p
                      ON p.person_source_value = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.PATID
                         )))
                    LEFT JOIN {visit_xwalk} vx
                      ON vx.encounterid = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.ENCOUNTERID
                         )))
                    WHERE r.target_domain = 'Condition'
                    """
                )
            )

            target_rows = int(
                con.execute(text(f"SELECT COUNT_BIG(*) FROM {condition}")).scalar_one()
            )
            target_max = int(
                con.execute(
                    text(f"SELECT COALESCE(MAX(condition_occurrence_id), 0) FROM {condition}")
                ).scalar_one()
            )
            obsclin_target_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {condition} c
                        JOIN {xwalk} x
                          ON x.condition_occurrence_id = c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                        """
                    )
                ).scalar_one()
            )
            obsclin_concept_zero = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {condition} c
                        JOIN {xwalk} x
                          ON x.condition_occurrence_id = c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                          AND c.condition_concept_id = 0
                        """
                    )
                ).scalar_one()
            )
            route_target_mismatch_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {routes} r
                        JOIN {xwalk} x
                          ON x.source_domain = 'OBS_CLIN'
                         AND x.source_record_id = r.source_obsclin_id
                        JOIN {condition} c
                          ON c.condition_occurrence_id = x.condition_occurrence_id
                        WHERE r.target_domain = 'Condition'
                          AND c.condition_concept_id <>
                              COALESCE(r.target_concept_id, 0)
                        """
                    )
                ).scalar_one()
            )
            visit_linked = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM {condition} c
                        JOIN {xwalk} x
                          ON x.condition_occurrence_id = c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                          AND c.visit_occurrence_id IS NOT NULL
                        """
                    )
                ).scalar_one()
            )

            if target_rows != expected_target_rows:
                raise RuntimeError(
                    "Final Condition row-count reconciliation failed: "
                    f"target={target_rows:,}, expected={expected_target_rows:,}"
                )
            if target_max != expected_target_max:
                raise RuntimeError(
                    "Final Condition ID-boundary reconciliation failed: "
                    f"max={target_max:,}, expected={expected_target_max:,}"
                )
            if obsclin_target_rows != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition target reconciliation failed: "
                    f"target={obsclin_target_rows:,}, routes={route_rows:,}"
                )
            if obsclin_concept_zero != route_concept_zero_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition concept-zero reconciliation failed: "
                    f"target={obsclin_concept_zero:,}, "
                    f"routes={route_concept_zero_rows:,}"
                )
            if route_target_mismatch_rows:
                raise RuntimeError(
                    "OBS_CLIN Condition target concepts differ from route ledger: "
                    f"{route_target_mismatch_rows:,} rows"
                )

        payload = {
            "stage": "condition_obs_clin_append",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "baseline_condition_rows": baseline_rows,
            "baseline_max_condition_occurrence_id": baseline_max,
            "obs_clin_condition_rows": route_rows,
            "route_distinct_source_rows": route_distinct_source_rows,
            "source_rows_resolved": source_rows_resolved,
            "target_rows": target_rows,
            "target_max_condition_occurrence_id": target_max,
            "concept_zero_rows": obsclin_concept_zero,
            "expected_concept_zero_rows": route_concept_zero_rows,
            "route_target_mismatch_rows": route_target_mismatch_rows,
            "invalid_standard_target_rows": invalid_standard_target_rows,
            "visit_linked_rows": visit_linked,
            "policy": (
                "OBS_CLIN rows are materialized from the domain route ledger; "
                "nonzero targets must be active Standard Condition concepts, "
                "and unresolved target_concept_id=0 is preserved when present."
            ),
            "status": "matched",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}

    finally:
        engine.dispose()
