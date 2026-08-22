from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


BASE_CONDITION_ROWS = 8_674_973
OBS_CLIN_CONDITION_ROWS = 39_115
FINAL_CONDITION_ROWS = 8_714_088

EXPECTED_TARGET_CONCEPT_ID = 4_185_946


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


def append_obs_clin_conditions(
    config: EtlConfig,
) -> dict[str, int | str]:
    engine = make_engine(config)
    audit_path = (
        config.audit_dir / "condition_obs_clin_append.json"
    )

    try:
        with engine.begin() as con:
            required = (
                "condition_occurrence",
                "etl_condition_occurrence_xwalk",
                "etl_obs_clin_route",
                "PCORnet_OBS_CLIN",
                "person",
                "etl_visit_occurrence_xwalk",
                "concept",
            )

            for table in required:
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(
                        f"Required table dbo.{table} does not exist"
                    )

            current_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.condition_occurrence
                    """)
                ).scalar_one()
            )

            current_max = int(
                con.execute(
                    text("""
                        SELECT COALESCE(
                            MAX(condition_occurrence_id), 0
                        )
                        FROM dbo.condition_occurrence
                    """)
                ).scalar_one()
            )

            xwalk_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_condition_occurrence_xwalk
                    """)
                ).scalar_one()
            )

            existing_obsclin_xwalk = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_condition_occurrence_xwalk
                        WHERE source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            if (
                current_rows == FINAL_CONDITION_ROWS
                and current_max == FINAL_CONDITION_ROWS
                and existing_obsclin_xwalk
                    == OBS_CLIN_CONDITION_ROWS
            ):
                return {
                    "status": "already_matched",
                    "target_rows": current_rows,
                    "obs_clin_rows": existing_obsclin_xwalk,
                    "audit_path": str(audit_path),
                }

            if (
                current_rows != BASE_CONDITION_ROWS
                or current_max != BASE_CONDITION_ROWS
                or xwalk_rows != BASE_CONDITION_ROWS
                or existing_obsclin_xwalk != 0
            ):
                raise RuntimeError(
                    "Unexpected pre-append Condition state: "
                    f"condition={current_rows:,}, "
                    f"max_id={current_max:,}, "
                    f"xwalk={xwalk_rows:,}, "
                    f"OBS_CLIN_xwalk="
                    f"{existing_obsclin_xwalk:,}"
                )

            route_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Condition'
                    """)
                ).scalar_one()
            )

            if route_rows != OBS_CLIN_CONDITION_ROWS:
                raise RuntimeError(
                    "Unexpected OBS_CLIN Condition route count: "
                    f"{route_rows:,}"
                )

            concept_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Condition'
                          AND target_concept_id = 0
                    """)
                ).scalar_one()
            )

            if concept_zero != 0:
                raise RuntimeError(
                    "OBS_CLIN Condition routes unexpectedly contain "
                    f"{concept_zero:,} concept-0 rows"
                )

            bad_target = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Condition'
                          AND target_concept_id <> :cid
                    """),
                    {"cid": EXPECTED_TARGET_CONCEPT_ID},
                ).scalar_one()
            )

            if bad_target != 0:
                raise RuntimeError(
                    "OBS_CLIN Condition routes no longer resolve "
                    "uniformly to the validated target concept"
                )

            concept_row = con.execute(
                text("""
                    SELECT
                        concept_id,
                        domain_id,
                        standard_concept,
                        invalid_reason
                    FROM dbo.concept
                    WHERE concept_id = :cid
                """),
                {"cid": EXPECTED_TARGET_CONCEPT_ID},
            ).one()

            if (
                concept_row[1] != "Condition"
                or concept_row[2] != "S"
                or concept_row[3] is not None
            ):
                raise RuntimeError(
                    "Validated Condition concept no longer has "
                    "expected semantics"
                )

            condition_datetime = _safe_datetime_sql(
                "o.OBSCLIN_START_DATE",
                "o.OBSCLIN_START_TIME",
            )

            # Append lineage first, within the same transaction.
            con.execute(
                text(f"""
                    INSERT INTO dbo.etl_condition_occurrence_xwalk (
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
                        {BASE_CONDITION_ROWS}
                        + ROW_NUMBER() OVER (
                            ORDER BY r.source_obsclin_id
                          ),
                        LEFT(CONVERT(
                            nvarchar(50), o.OBSCLIN_TYPE
                        ), 50),
                        LEFT(CONVERT(
                            nvarchar(50), o.OBSCLIN_SOURCE
                        ), 50),
                        'OBSCLIN_START_DATE'
                    FROM dbo.etl_obs_clin_route r
                    JOIN dbo.PCORnet_OBS_CLIN o
                      ON r.source_obsclin_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    WHERE r.target_domain = 'Condition'
                """)
            )

            new_xwalk_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_condition_occurrence_xwalk
                        WHERE source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            if new_xwalk_rows != OBS_CLIN_CONDITION_ROWS:
                raise RuntimeError(
                    "OBS_CLIN Condition xwalk reconciliation failed"
                )

            con.execute(
                text(f"""
                    INSERT INTO dbo.condition_occurrence (
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
                        CONVERT(int, r.target_concept_id),
                        CAST(o.OBSCLIN_START_DATE AS date),
                        {condition_datetime},
                        CAST(o.OBSCLIN_STOP_DATE AS date),
                        CASE
                            WHEN o.OBSCLIN_STOP_DATE IS NULL
                                THEN NULL
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
                        LEFT(CONVERT(
                            varchar(50), o.OBSCLIN_CODE
                        ), 50),
                        CONVERT(int, r.source_concept_id),
                        NULL
                    FROM dbo.etl_obs_clin_route r
                    JOIN dbo.PCORnet_OBS_CLIN o
                      ON r.source_obsclin_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), o.OBSCLINID
                         )))
                    JOIN dbo.etl_condition_occurrence_xwalk x
                      ON x.source_domain = 'OBS_CLIN'
                     AND x.source_record_id =
                         r.source_obsclin_id
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
                    WHERE r.target_domain = 'Condition'
                """)
            )

            target_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.condition_occurrence
                    """)
                ).scalar_one()
            )

            target_max = int(
                con.execute(
                    text("""
                        SELECT MAX(condition_occurrence_id)
                        FROM dbo.condition_occurrence
                    """)
                ).scalar_one()
            )

            obsclin_target_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.condition_occurrence c
                        JOIN dbo.etl_condition_occurrence_xwalk x
                          ON x.condition_occurrence_id =
                             c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            obsclin_concept_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.condition_occurrence c
                        JOIN dbo.etl_condition_occurrence_xwalk x
                          ON x.condition_occurrence_id =
                             c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                          AND c.condition_concept_id = 0
                    """)
                ).scalar_one()
            )

            visit_linked = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.condition_occurrence c
                        JOIN dbo.etl_condition_occurrence_xwalk x
                          ON x.condition_occurrence_id =
                             c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                          AND c.visit_occurrence_id IS NOT NULL
                    """)
                ).scalar_one()
            )

            if target_rows != FINAL_CONDITION_ROWS:
                raise RuntimeError(
                    "Final condition row count mismatch: "
                    f"{target_rows:,} != {FINAL_CONDITION_ROWS:,}"
                )

            if target_max != FINAL_CONDITION_ROWS:
                raise RuntimeError(
                    "Final condition ID boundary mismatch: "
                    f"{target_max:,} != {FINAL_CONDITION_ROWS:,}"
                )

            if obsclin_target_rows != OBS_CLIN_CONDITION_ROWS:
                raise RuntimeError(
                    "OBS_CLIN Condition target reconciliation failed"
                )

            if obsclin_concept_zero != 0:
                raise RuntimeError(
                    "Unexpected concept-0 rows in OBS_CLIN "
                    "Condition append"
                )

        payload = {
            "stage": "condition_obs_clin_append",
            "recorded_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "baseline_condition_rows": BASE_CONDITION_ROWS,
            "obs_clin_condition_rows": OBS_CLIN_CONDITION_ROWS,
            "target_rows": target_rows,
            "target_max_condition_occurrence_id": target_max,
            "target_concept_id": EXPECTED_TARGET_CONCEPT_ID,
            "concept_zero_rows": obsclin_concept_zero,
            "visit_linked_rows": visit_linked,
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
