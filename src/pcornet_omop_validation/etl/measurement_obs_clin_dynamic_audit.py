from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


OVERFLOW_TABLE = "etl_measurement_obsclin_text_overflow"


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def audit_measurement_obs_clin_dynamic(config: EtlConfig) -> dict[str, object]:
    """Audit OBS_CLIN Measurement materialization from source-derived rules.

    No site-specific row counts are assumed. Expected counts are derived from
    the OBS_CLIN route ledger and source data currently present in the database.
    The audit is read-only except for writing its local JSON result.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "measurement_obs_clin_dynamic_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "measurement"),
                (target_schema, "etl_measurement_xwalk"),
                (target_schema, "etl_obs_clin_route"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            route_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route]
                WHERE target_domain = 'Measurement'
            """)).scalar_one())

            route_distinct_source_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(DISTINCT source_obsclin_id)
                FROM [{target_schema}].[etl_obs_clin_route]
                WHERE target_domain = 'Measurement'
            """)).scalar_one())

            source_rows_resolved = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] o
                  ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                WHERE r.target_domain = 'Measurement'
            """)).scalar_one())

            xwalk_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_measurement_xwalk]
                WHERE source_family = 'OBS_CLIN'
            """)).scalar_one())

            xwalk_distinct_source_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(DISTINCT source_record_id)
                FROM [{target_schema}].[etl_measurement_xwalk]
                WHERE source_family = 'OBS_CLIN'
            """)).scalar_one())

            target_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{target_schema}].[etl_measurement_xwalk] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'OBS_CLIN'
            """)).scalar_one())

            route_target_mismatch_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                JOIN [{target_schema}].[etl_measurement_xwalk] x
                  ON x.source_family = 'OBS_CLIN'
                 AND x.source_route_id = r.route_id
                JOIN [{target_schema}].[measurement] m
                  ON m.measurement_id = x.measurement_id
                WHERE r.target_domain = 'Measurement'
                  AND COALESCE(m.measurement_concept_id, 0) <>
                      COALESCE(r.target_concept_id, 0)
            """)).scalar_one())

            route_concept_zero_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route]
                WHERE target_domain = 'Measurement'
                  AND COALESCE(target_concept_id, 0) = 0
            """)).scalar_one())

            target_concept_zero_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{target_schema}].[etl_measurement_xwalk] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'OBS_CLIN'
                  AND COALESCE(m.measurement_concept_id, 0) = 0
            """)).scalar_one())

            invalid_standard_target_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_domain = 'Measurement'
                  AND COALESCE(r.target_concept_id, 0) <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Measurement'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
            """)).scalar_one())

            expected_overflow_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[etl_obs_clin_route] r
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] o
                  ON r.source_obsclin_id = LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID)))
                WHERE r.target_domain = 'Measurement'
                  AND LEN(COALESCE(
                        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(max), o.RAW_OBSCLIN_RESULT))), ''),
                        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(max), o.OBSCLIN_RESULT_TEXT))), '')
                      )) > 50
            """)).scalar_one())

            overflow_rows = None
            if table_exists(con, target_schema, OVERFLOW_TABLE):
                overflow_rows = int(con.execute(text(
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{OVERFLOW_TABLE}]"
                )).scalar_one())

            route_target_concepts = [
                {
                    "target_concept_id": int(r[0]),
                    "rows": int(r[1]),
                    "concept_name": r[2],
                    "vocabulary_id": r[3],
                    "concept_code": r[4],
                }
                for r in con.execute(text(f"""
                    SELECT
                        COALESCE(r.target_concept_id, 0),
                        COUNT_BIG(*),
                        MAX(c.concept_name),
                        MAX(c.vocabulary_id),
                        MAX(c.concept_code)
                    FROM [{target_schema}].[etl_obs_clin_route] r
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = r.target_concept_id
                    WHERE r.target_domain = 'Measurement'
                    GROUP BY COALESCE(r.target_concept_id, 0)
                    ORDER BY COUNT_BIG(*) DESC
                """)).fetchall()
            ]

        checks = {
            "route_vs_source": route_rows == source_rows_resolved,
            "route_vs_xwalk": route_rows == xwalk_rows,
            "xwalk_vs_target": xwalk_rows == target_rows,
            "route_source_uniqueness": route_rows == route_distinct_source_rows,
            "xwalk_source_uniqueness": xwalk_rows == xwalk_distinct_source_rows,
            "route_target_match": route_target_mismatch_rows == 0,
            "concept_zero_match": route_concept_zero_rows == target_concept_zero_rows,
            "standard_measurement_semantics": invalid_standard_target_rows == 0,
            "overflow_match": overflow_rows == expected_overflow_rows,
        }
        status = "matched" if all(checks.values()) else "review_required"

        payload: dict[str, object] = {
            "stage": "measurement_obs_clin_dynamic_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "route_rows": route_rows,
            "route_distinct_source_rows": route_distinct_source_rows,
            "source_rows_resolved": source_rows_resolved,
            "xwalk_rows": xwalk_rows,
            "xwalk_distinct_source_rows": xwalk_distinct_source_rows,
            "target_rows": target_rows,
            "route_target_mismatch_rows": route_target_mismatch_rows,
            "route_concept_zero_rows": route_concept_zero_rows,
            "target_concept_zero_rows": target_concept_zero_rows,
            "invalid_standard_target_rows": invalid_standard_target_rows,
            "expected_overflow_rows": expected_overflow_rows,
            "overflow_rows": overflow_rows,
            "checks": checks,
            "route_target_concepts": route_target_concepts,
            "status": status,
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        payload["audit_path"] = str(audit_path)
        return payload
    finally:
        engine.dispose()
