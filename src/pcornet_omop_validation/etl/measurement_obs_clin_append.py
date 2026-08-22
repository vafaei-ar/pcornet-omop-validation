from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


BASE_MEASUREMENT_ROWS = 48_217_976
OBS_CLIN_MEASUREMENT_ROWS = 37_340_715
FINAL_MEASUREMENT_ROWS = 85_558_691
EXPECTED_CONCEPT_ZERO = 2_616
EXPECTED_UNIT_ZERO = 13_938_742
EXPECTED_TEXT_OVERFLOW = 22_887

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


def append_obs_clin_measurements(
    config: EtlConfig,
) -> dict[str, int | str]:
    engine = make_engine(config)
    audit_path = config.audit_dir / "measurement_obs_clin_append.json"

    try:
        with engine.connect() as con:
            required = (
                "measurement",
                "etl_measurement_xwalk",
                "etl_obs_clin_route",
                "PCORnet_OBS_CLIN",
                "person",
                "etl_visit_occurrence_xwalk",
            )
            for table in required:
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(
                        f"Required table dbo.{table} does not exist"
                    )

            current_rows = int(
                con.execute(
                    text("SELECT COUNT_BIG(*) FROM dbo.measurement")
                ).scalar_one()
            )
            current_max = int(
                con.execute(
                    text(
                        "SELECT COALESCE(MAX(measurement_id),0) "
                        "FROM dbo.measurement"
                    )
                ).scalar_one()
            )
            xwalk_rows = int(
                con.execute(
                    text(
                        "SELECT COUNT_BIG(*) "
                        "FROM dbo.etl_measurement_xwalk"
                    )
                ).scalar_one()
            )
            obs_xwalk_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_measurement_xwalk
                        WHERE source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            # Clean successful rerun recognition.
            if (
                current_rows == FINAL_MEASUREMENT_ROWS
                and current_max == FINAL_MEASUREMENT_ROWS
                and obs_xwalk_rows == OBS_CLIN_MEASUREMENT_ROWS
            ):
                return {
                    "status": "already_matched",
                    "target_rows": current_rows,
                    "obs_clin_rows": obs_xwalk_rows,
                    "audit_path": str(audit_path),
                }

            if (
                current_rows != BASE_MEASUREMENT_ROWS
                or current_max != BASE_MEASUREMENT_ROWS
                or xwalk_rows != BASE_MEASUREMENT_ROWS
                or obs_xwalk_rows != 0
            ):
                raise RuntimeError(
                    "Unexpected pre-append Measurement state: "
                    f"measurement={current_rows:,}, "
                    f"max_id={current_max:,}, "
                    f"xwalk={xwalk_rows:,}, "
                    f"OBS_CLIN_xwalk={obs_xwalk_rows:,}"
                )

            route_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Measurement'
                    """)
                ).scalar_one()
            )
            if route_rows != OBS_CLIN_MEASUREMENT_ROWS:
                raise RuntimeError(
                    "Unexpected OBS_CLIN Measurement route count: "
                    f"{route_rows:,}"
                )

            if table_exists(con, "dbo", OVERFLOW_TABLE):
                raise RuntimeError(
                    f"dbo.{OVERFLOW_TABLE} already exists"
                )

            # Append deterministic lineage.
            con.execute(
                text(f"""
                    INSERT INTO dbo.etl_measurement_xwalk (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        {BASE_MEASUREMENT_ROWS}
                        + ROW_NUMBER() OVER (
                            ORDER BY r.source_obsclin_id
                          ) AS measurement_id,
                        'OBS_CLIN',
                        r.source_obsclin_id,
                        NULL,
                        r.route_id
                    FROM dbo.etl_obs_clin_route r
                    WHERE r.target_domain = 'Measurement'
                """)
            )
            con.commit()

            obs_xwalk_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_measurement_xwalk
                        WHERE source_family = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )
            if obs_xwalk_rows != OBS_CLIN_MEASUREMENT_ROWS:
                raise RuntimeError(
                    "OBS_CLIN Measurement xwalk reconciliation failed"
                )

            measurement_datetime = _safe_datetime(
                "o.OBSCLIN_START_DATE",
                "o.OBSCLIN_START_TIME",
            )

            # OBSCLIN_RESULT_UNIT is a standardized mixed-case UCUM field.
            # Resolve only exact, case-sensitive, unique active Standard
            # Unit concepts. Otherwise preserve the source string and use 0.
            unit_cte_sql = """
                WITH unit_candidates AS (
                    SELECT
                        c.concept_code COLLATE Latin1_General_100_BIN2
                            AS concept_code,
                        c.concept_id,
                        COUNT_BIG(*) OVER (
                            PARTITION BY
                                c.concept_code COLLATE Latin1_General_100_BIN2
                        ) AS candidate_count
                    FROM dbo.concept c
                    WHERE c.vocabulary_id = 'UCUM'
                      AND c.domain_id = 'Unit'
                      AND c.standard_concept = 'S'
                      AND c.invalid_reason IS NULL
                ),
                unit_map AS (
                    SELECT
                        concept_code,
                        MAX(
                            CASE WHEN candidate_count = 1
                                 THEN concept_id ELSE NULL END
                        ) AS unit_concept_id
                    FROM unit_candidates
                    GROUP BY concept_code
                )
            """

            # RAW result is preferred, then normalized text.
            # RESULT_QUAL is intentionally not used as a value.
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

            con.execute(
                text(
                    unit_cte_sql
                    + f"""
                    INSERT INTO dbo.measurement (
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
                        CONVERT(int, r.target_concept_id),
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
                        LEFT(CONVERT(
                            varchar(50), o.OBSCLIN_CODE
                        ), 50),
                        CONVERT(int, r.source_concept_id),
                        LEFT(CONVERT(
                            varchar(50), o.OBSCLIN_RESULT_UNIT
                        ), 50),
                        COALESCE(um.unit_concept_id, 0),
                        LEFT(CONVERT(
                            varchar(max), {source_value_sql}
                        ), 50),
                        NULL,
                        0
                    FROM dbo.etl_obs_clin_route r
                    JOIN dbo.PCORnet_OBS_CLIN o
                      ON r.source_obsclin_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    LEFT JOIN unit_map um
                      ON um.concept_code =
                         NULLIF(LTRIM(RTRIM(CONVERT(
                           nvarchar(50), o.OBSCLIN_RESULT_UNIT
                         ))), '') COLLATE Latin1_General_100_BIN2
                    JOIN dbo.etl_measurement_xwalk x
                      ON x.source_family = 'OBS_CLIN'
                     AND x.source_record_id = r.source_obsclin_id
                    JOIN dbo.person p
                      ON p.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.PATID
                         )))
                    LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.ENCOUNTERID
                         )))
                    WHERE r.target_domain = 'Measurement'
                """
                )
            )
            con.commit()

            # Preserve full source values only where OMOP's varchar(50)
            # projection necessarily truncates them.
            con.execute(
                text(f"""
                    CREATE TABLE dbo.{OVERFLOW_TABLE} (
                        measurement_id bigint NOT NULL PRIMARY KEY,
                        source_obsclin_id nvarchar(255) NOT NULL,
                        source_length int NOT NULL,
                        projected_value varchar(50) NULL,
                        full_source_value nvarchar(max) NOT NULL
                    )
                """)
            )
            con.commit()

            con.execute(
                text(f"""
                    INSERT INTO dbo.{OVERFLOW_TABLE} (
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
                        LEFT(CONVERT(
                            varchar(max), {source_value_sql}
                        ), 50),
                        {source_value_sql}
                    FROM dbo.etl_obs_clin_route r
                    JOIN dbo.PCORnet_OBS_CLIN o
                      ON r.source_obsclin_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    JOIN dbo.etl_measurement_xwalk x
                      ON x.source_family = 'OBS_CLIN'
                     AND x.source_record_id = r.source_obsclin_id
                    WHERE r.target_domain = 'Measurement'
                      AND LEN({source_value_sql}) > 50
                """)
            )
            con.commit()

            target_rows = int(
                con.execute(
                    text("SELECT COUNT_BIG(*) FROM dbo.measurement")
                ).scalar_one()
            )
            target_max = int(
                con.execute(
                    text("SELECT MAX(measurement_id) FROM dbo.measurement")
                ).scalar_one()
            )
            concept_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.measurement m
                        JOIN dbo.etl_measurement_xwalk x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.measurement_concept_id = 0
                    """)
                ).scalar_one()
            )
            unit_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.measurement m
                        JOIN dbo.etl_measurement_xwalk x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.unit_concept_id = 0
                    """)
                ).scalar_one()
            )
            overflow_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) "
                        f"FROM dbo.{OVERFLOW_TABLE}"
                    )
                ).scalar_one()
            )
            visit_linked = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.measurement m
                        JOIN dbo.etl_measurement_xwalk x
                          ON x.measurement_id = m.measurement_id
                        WHERE x.source_family = 'OBS_CLIN'
                          AND m.visit_occurrence_id IS NOT NULL
                    """)
                ).scalar_one()
            )

            checks = {
                "target_rows": (
                    target_rows,
                    FINAL_MEASUREMENT_ROWS,
                ),
                "target_max_id": (
                    target_max,
                    FINAL_MEASUREMENT_ROWS,
                ),
                "concept_zero": (
                    concept_zero,
                    EXPECTED_CONCEPT_ZERO,
                ),
                "unit_zero": (
                    unit_zero,
                    EXPECTED_UNIT_ZERO,
                ),
                "text_overflow": (
                    overflow_rows,
                    EXPECTED_TEXT_OVERFLOW,
                ),
            }

            failed = {
                k: v
                for k, v in checks.items()
                if v[0] != v[1]
            }
            if failed:
                raise RuntimeError(
                    f"OBS_CLIN Measurement reconciliation failed: {failed}"
                )

        payload = {
            "stage": "measurement_obs_clin_append",
            "recorded_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "baseline_measurement_rows": BASE_MEASUREMENT_ROWS,
            "obs_clin_measurement_rows": OBS_CLIN_MEASUREMENT_ROWS,
            "target_rows": target_rows,
            "target_max_measurement_id": target_max,
            "measurement_concept_zero_rows": concept_zero,
            "unit_concept_zero_rows": unit_zero,
            "value_source_overflow_rows": overflow_rows,
            "visit_linked_rows": visit_linked,
            "unit_policy": (
                "Exact case-sensitive unique active Standard UCUM Unit "
                "concept; otherwise concept_id=0 with source unit preserved"
            ),
            "value_policy": (
                "RAW_OBSCLIN_RESULT then OBSCLIN_RESULT_TEXT; "
                "RESULT_QUAL is not substituted; OMOP projection "
                "is explicitly limited to 50 characters with full "
                "overflow retained locally"
            ),
            "status": "matched",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return {
            **payload,
            "audit_path": str(audit_path),
        }

    finally:
        engine.dispose()
