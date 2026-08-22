from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


OVERFLOW_TABLE = "etl_measurement_obsclin_text_overflow"


def _safe_datetime(date_expr: str, time_expr: str) -> str:
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


def _validated_schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def append_obs_clin_measurements(config: EtlConfig) -> dict[str, int | str]:
    engine = make_engine(config)
    audit_path = config.audit_dir / "measurement_obs_clin_append.json"

    sql_cfg = config.raw["sqlserver"]
    source_schema = _validated_schema(
        sql_cfg.get("source_schema", "dbo"), "source_schema"
    )
    target_schema = _validated_schema(
        sql_cfg.get("target_schema", "dbo"), "target_schema"
    )

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    try:
        with engine.begin() as con:
            source_required = ("PCORnet_OBS_CLIN",)
            target_required = (
                "measurement",
                "etl_measurement_xwalk",
                "etl_obs_clin_route",
                "person",
                "etl_visit_occurrence_xwalk",
                "concept",
            )
            for table in source_required:
                if not table_exists(con, source_schema, table):
                    raise RuntimeError(
                        f"Required table {source_schema}.{table} does not exist"
                    )
            for table in target_required:
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(
                        f"Required table {target_schema}.{table} does not exist"
                    )

            route_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')}
                        WHERE target_domain = 'Measurement'
                    """)
                ).scalar_one()
            )
            route_distinct_sources = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(DISTINCT source_obsclin_id)
                        FROM {t('etl_obs_clin_route')}
                        WHERE target_domain = 'Measurement'
                    """)
                ).scalar_one()
            )
            if route_rows != route_distinct_sources:
                raise RuntimeError(
                    "OBS_CLIN Measurement routing is not one row per source event: "
                    f"routes={route_rows:,}, distinct_sources={route_distinct_sources:,}"
                )

            unresolved_source_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')} r
                        LEFT JOIN {s('PCORnet_OBS_CLIN')} o
                          ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                               nvarchar(255), o.OBSCLINID
                             )))
                        WHERE r.target_domain = 'Measurement'
                          AND o.OBSCLINID IS NULL
                    """)
                ).scalar_one()
            )
            if unresolved_source_rows:
                raise RuntimeError(
                    "OBS_CLIN Measurement routes have missing source rows: "
                    f"{unresolved_source_rows:,}"
                )

            invalid_standard_targets = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')} r
                        LEFT JOIN {t('concept')} c
                          ON c.concept_id = r.target_concept_id
                        WHERE r.target_domain = 'Measurement'
                          AND COALESCE(r.target_concept_id, 0) <> 0
                          AND (
                              c.concept_id IS NULL
                              OR c.domain_id <> 'Measurement'
                              OR c.standard_concept <> 'S'
                              OR c.invalid_reason IS NOT NULL
                          )
                    """)
                ).scalar_one()
            )
            if invalid_standard_targets:
                raise RuntimeError(
                    "OBS_CLIN Measurement routes contain invalid nonzero targets: "
                    f"{invalid_standard_targets:,}"
                )

            expected_concept_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')}
                        WHERE target_domain = 'Measurement'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
            )

            unit_cte_sql = f"""
                WITH unit_candidates AS (
                    SELECT
                        c.concept_code COLLATE Latin1_General_100_BIN2
                            AS concept_code,
                        c.concept_id,
                        COUNT_BIG(*) OVER (
                            PARTITION BY
                                c.concept_code COLLATE Latin1_General_100_BIN2
                        ) AS candidate_count
                    FROM {t('concept')} c
                    WHERE c.vocabulary_id = 'UCUM'
                      AND c.domain_id = 'Unit'
                      AND c.standard_concept = 'S'
                      AND c.invalid_reason IS NULL
                ),
                unit_map AS (
                    SELECT
                        concept_code,
                        MAX(CASE WHEN candidate_count = 1
                                 THEN concept_id ELSE NULL END) AS unit_concept_id
                    FROM unit_candidates
                    GROUP BY concept_code
                )
            """

            expected_unit_zero = int(
                con.execute(
                    text(
                        unit_cte_sql
                        + f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')} r
                        JOIN {s('PCORnet_OBS_CLIN')} o
                          ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                               nvarchar(255), o.OBSCLINID
                             )))
                        LEFT JOIN unit_map um
                          ON um.concept_code = NULLIF(LTRIM(RTRIM(CONVERT(
                               nvarchar(50), o.OBSCLIN_RESULT_UNIT
                             ))), '') COLLATE Latin1_General_100_BIN2
                        WHERE r.target_domain = 'Measurement'
                          AND um.unit_concept_id IS NULL
                        """
                    )
                ).scalar_one()
            )

            source_value_sql = """
                COALESCE(
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.RAW_OBSCLIN_RESULT
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSCLIN_RESULT_TEXT
                    ))), '')
                )
            """
            expected_overflow = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_obs_clin_route')} r
                        JOIN {s('PCORnet_OBS_CLIN')} o
                          ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                               nvarchar(255), o.OBSCLINID
                             )))
                        WHERE r.target_domain = 'Measurement'
                          AND LEN({source_value_sql}) > 50
                    """)
                ).scalar_one()
            )

            current_rows = int(
                con.execute(
                    text(f"SELECT COUNT_BIG(*) FROM {t('measurement')}")
                ).scalar_one()
            )
            current_max = int(
                con.execute(
                    text(
                        f"SELECT COALESCE(MAX(measurement_id), 0) "
                        f"FROM {t('measurement')}"
                    )
                ).scalar_one()
            )
            xwalk_rows = int(
                con.execute(
                    text(f"SELECT COUNT_BIG(*) FROM {t('etl_measurement_xwalk')}")
                ).scalar_one()
            )
            obs_xwalk_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_measurement_xwalk')}
                        WHERE source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )
            obs_target_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('measurement')} m
                        JOIN {t('etl_measurement_xwalk')} x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            if obs_xwalk_rows:
                route_target_mismatch = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM {t('etl_obs_clin_route')} r
                            JOIN {t('etl_measurement_xwalk')} x
                              ON x.source_family = 'OBS_CLIN'
                             AND x.source_record_id = r.source_obsclin_id
                            JOIN {t('measurement')} m
                              ON m.measurement_id = x.measurement_id
                            WHERE r.target_domain = 'Measurement'
                              AND m.measurement_concept_id <>
                                  COALESCE(r.target_concept_id, 0)
                        """)
                    ).scalar_one()
                )
                actual_concept_zero = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM {t('measurement')} m
                            JOIN {t('etl_measurement_xwalk')} x
                              ON x.measurement_id = m.measurement_id
                            WHERE x.source_family = 'OBS_CLIN'
                              AND m.measurement_concept_id = 0
                        """)
                    ).scalar_one()
                )
                actual_unit_zero = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM {t('measurement')} m
                            JOIN {t('etl_measurement_xwalk')} x
                              ON x.measurement_id = m.measurement_id
                            WHERE x.source_family = 'OBS_CLIN'
                              AND m.unit_concept_id = 0
                        """)
                    ).scalar_one()
                )
                overflow_rows = (
                    int(
                        con.execute(
                            text(
                                f"SELECT COUNT_BIG(*) FROM {t(OVERFLOW_TABLE)}"
                            )
                        ).scalar_one()
                    )
                    if table_exists(con, target_schema, OVERFLOW_TABLE)
                    else 0
                )
                matched = (
                    obs_xwalk_rows == route_rows
                    and obs_target_rows == route_rows
                    and route_target_mismatch == 0
                    and actual_concept_zero == expected_concept_zero
                    and actual_unit_zero == expected_unit_zero
                    and overflow_rows == expected_overflow
                )
                if not matched:
                    raise RuntimeError(
                        "Existing OBS_CLIN Measurement materialization does not "
                        "match the current route ledger/source-derived semantics"
                    )
                return {
                    "status": "already_matched",
                    "baseline_measurement_rows": current_rows - obs_target_rows,
                    "obs_clin_measurement_rows": obs_target_rows,
                    "target_rows": current_rows,
                    "target_max_measurement_id": current_max,
                    "concept_zero_rows": actual_concept_zero,
                    "unit_concept_zero_rows": actual_unit_zero,
                    "value_source_overflow_rows": overflow_rows,
                    "audit_path": str(audit_path),
                }

            if obs_target_rows != 0:
                raise RuntimeError(
                    "OBS_CLIN Measurement target rows exist without lineage"
                )
            if xwalk_rows != current_rows:
                raise RuntimeError(
                    "Pre-append Measurement lineage count does not match target: "
                    f"xwalk={xwalk_rows:,}, measurement={current_rows:,}"
                )
            if table_exists(con, target_schema, OVERFLOW_TABLE):
                raise RuntimeError(
                    f"{target_schema}.{OVERFLOW_TABLE} already exists before append"
                )

            baseline_rows = current_rows
            baseline_max = current_max

            con.execute(
                text(f"""
                    INSERT INTO {t('etl_measurement_xwalk')} (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        {baseline_max}
                        + ROW_NUMBER() OVER (ORDER BY r.source_obsclin_id),
                        'OBS_CLIN',
                        r.source_obsclin_id,
                        NULL,
                        r.route_id
                    FROM {t('etl_obs_clin_route')} r
                    WHERE r.target_domain = 'Measurement'
                """)
            )

            obs_xwalk_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('etl_measurement_xwalk')}
                        WHERE source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )
            if obs_xwalk_rows != route_rows:
                raise RuntimeError(
                    "OBS_CLIN Measurement xwalk reconciliation failed: "
                    f"{obs_xwalk_rows:,} != {route_rows:,}"
                )

            measurement_datetime = _safe_datetime(
                "o.OBSCLIN_START_DATE", "o.OBSCLIN_START_TIME"
            )

            con.execute(
                text(
                    unit_cte_sql
                    + f"""
                    INSERT INTO {t('measurement')} (
                        measurement_id,
                        person_id,
                        measurement_concept_id,
                        measurement_date,
                        measurement_datetime,
                        measurement_time,
                        measurement_type_concept_id,
                        operator_concept_id,
                        value_as_number,
                        value_as_concept_id,
                        unit_concept_id,
                        range_low,
                        range_high,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        measurement_source_value,
                        measurement_source_concept_id,
                        unit_source_value,
                        unit_source_concept_id,
                        value_source_value,
                        measurement_event_id,
                        meas_event_field_concept_id
                    )
                    SELECT
                        x.measurement_id,
                        p.person_id,
                        COALESCE(CONVERT(int, r.target_concept_id), 0),
                        CAST(o.OBSCLIN_START_DATE AS date),
                        {measurement_datetime},
                        NULL,
                        0,
                        0,
                        TRY_CONVERT(float, o.OBSCLIN_RESULT_NUM),
                        0,
                        COALESCE(um.unit_concept_id, 0),
                        NULL,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), o.OBSCLIN_CODE), 50),
                        COALESCE(CONVERT(int, r.source_concept_id), 0),
                        LEFT(CONVERT(varchar(50), o.OBSCLIN_RESULT_UNIT), 50),
                        COALESCE(um.unit_concept_id, 0),
                        LEFT(CONVERT(varchar(max), {source_value_sql}), 50),
                        NULL,
                        0
                    FROM {t('etl_obs_clin_route')} r
                    JOIN {s('PCORnet_OBS_CLIN')} o
                      ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    LEFT JOIN unit_map um
                      ON um.concept_code = NULLIF(LTRIM(RTRIM(CONVERT(
                           nvarchar(50), o.OBSCLIN_RESULT_UNIT
                         ))), '') COLLATE Latin1_General_100_BIN2
                    JOIN {t('etl_measurement_xwalk')} x
                      ON x.source_family = 'OBS_CLIN'
                     AND x.source_record_id = r.source_obsclin_id
                    JOIN {t('person')} p
                      ON p.person_source_value = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.PATID
                         )))
                    LEFT JOIN {t('etl_visit_occurrence_xwalk')} vx
                      ON vx.encounterid = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.ENCOUNTERID
                         )))
                    WHERE r.target_domain = 'Measurement'
                    """
                )
            )

            con.exec_driver_sql(f"""
                CREATE TABLE {t(OVERFLOW_TABLE)} (
                    measurement_id bigint NOT NULL PRIMARY KEY,
                    source_obsclin_id nvarchar(255) NOT NULL,
                    source_length int NOT NULL,
                    projected_value varchar(50) NULL,
                    full_source_value nvarchar(max) NOT NULL
                )
            """)
            con.execute(
                text(f"""
                    INSERT INTO {t(OVERFLOW_TABLE)} (
                        measurement_id,
                        source_obsclin_id,
                        source_length,
                        projected_value,
                        full_source_value
                    )
                    SELECT
                        x.measurement_id,
                        r.source_obsclin_id,
                        LEN({source_value_sql}),
                        LEFT(CONVERT(varchar(max), {source_value_sql}), 50),
                        {source_value_sql}
                    FROM {t('etl_obs_clin_route')} r
                    JOIN {s('PCORnet_OBS_CLIN')} o
                      ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    JOIN {t('etl_measurement_xwalk')} x
                      ON x.source_family = 'OBS_CLIN'
                     AND x.source_record_id = r.source_obsclin_id
                    WHERE r.target_domain = 'Measurement'
                      AND LEN({source_value_sql}) > 50
                """)
            )

            target_rows = int(
                con.execute(
                    text(f"SELECT COUNT_BIG(*) FROM {t('measurement')}")
                ).scalar_one()
            )
            target_max = int(
                con.execute(
                    text(f"SELECT MAX(measurement_id) FROM {t('measurement')}")
                ).scalar_one()
            )
            obs_target_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('measurement')} m
                        JOIN {t('etl_measurement_xwalk')} x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )
            concept_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('measurement')} m
                        JOIN {t('etl_measurement_xwalk')} x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.measurement_concept_id = 0
                    """)
                ).scalar_one()
            )
            unit_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('measurement')} m
                        JOIN {t('etl_measurement_xwalk')} x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.unit_concept_id = 0
                    """)
                ).scalar_one()
            )
            overflow_rows = int(
                con.execute(
                    text(f"SELECT COUNT_BIG(*) FROM {t(OVERFLOW_TABLE)}")
                ).scalar_one()
            )
            visit_linked = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM {t('measurement')} m
                        JOIN {t('etl_measurement_xwalk')} x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.visit_occurrence_id IS NOT NULL
                    """)
                ).scalar_one()
            )

            expected_target_rows = baseline_rows + route_rows
            expected_target_max = baseline_max + route_rows
            checks = {
                "target_rows": (target_rows, expected_target_rows),
                "target_max_id": (target_max, expected_target_max),
                "obs_clin_target_rows": (obs_target_rows, route_rows),
                "concept_zero": (concept_zero, expected_concept_zero),
                "unit_zero": (unit_zero, expected_unit_zero),
                "text_overflow": (overflow_rows, expected_overflow),
            }
            failed = {k: v for k, v in checks.items() if v[0] != v[1]}
            if failed:
                raise RuntimeError(
                    f"OBS_CLIN Measurement reconciliation failed: {failed}"
                )

        payload = {
            "stage": "measurement_obs_clin_append",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "baseline_measurement_rows": baseline_rows,
            "baseline_max_measurement_id": baseline_max,
            "obs_clin_measurement_rows": route_rows,
            "target_rows": target_rows,
            "target_max_measurement_id": target_max,
            "measurement_concept_zero_rows": concept_zero,
            "expected_measurement_concept_zero_rows": expected_concept_zero,
            "unit_concept_zero_rows": unit_zero,
            "expected_unit_concept_zero_rows": expected_unit_zero,
            "value_source_overflow_rows": overflow_rows,
            "expected_value_source_overflow_rows": expected_overflow,
            "visit_linked_rows": visit_linked,
            "unit_policy": (
                "Exact case-sensitive unique active Standard UCUM Unit concept; "
                "otherwise concept_id=0 with source unit preserved"
            ),
            "value_policy": (
                "RAW_OBSCLIN_RESULT then OBSCLIN_RESULT_TEXT; RESULT_QUAL is not "
                "substituted; OMOP projection is limited to 50 characters with "
                "full overflow retained locally"
            ),
            "status": "matched",
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
