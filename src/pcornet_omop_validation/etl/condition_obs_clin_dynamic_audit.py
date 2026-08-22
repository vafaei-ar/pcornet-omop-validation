from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


def audit_condition_obs_clin_dynamic(config: EtlConfig) -> dict[str, object]:
    """Audit OBS_CLIN-derived Condition rows without site-specific constants.

    Reconcile the source-domain route ledger, condition lineage, materialized
    Condition rows, and vocabulary semantics using only configured schemas and
    current source-derived counts.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_obs_clin_dynamic_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "etl_obs_clin_route"),
                (target_schema, "etl_condition_occurrence_xwalk"),
                (target_schema, "condition_occurrence"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            route_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route]
                        WHERE target_domain = 'Condition'
                    """)
                ).scalar_one()
            )

            route_distinct_source_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(DISTINCT source_obsclin_id)
                        FROM [{target_schema}].[etl_obs_clin_route]
                        WHERE target_domain = 'Condition'
                    """)
                ).scalar_one()
            )

            source_rows_resolved = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route] r
                        JOIN [{source_schema}].[PCORnet_OBS_CLIN] o
                          ON r.source_obsclin_id =
                             LTRIM(RTRIM(CONVERT(
                               nvarchar(255), o.OBSCLINID
                             )))
                        WHERE r.target_domain = 'Condition'
                    """)
                ).scalar_one()
            )

            xwalk_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_condition_occurrence_xwalk]
                        WHERE source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            xwalk_distinct_source_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(DISTINCT source_record_id)
                        FROM [{target_schema}].[etl_condition_occurrence_xwalk]
                        WHERE source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            target_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[condition_occurrence] c
                        JOIN [{target_schema}].[etl_condition_occurrence_xwalk] x
                          ON x.condition_occurrence_id = c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                    """)
                ).scalar_one()
            )

            route_target_mismatch_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route] r
                        JOIN [{target_schema}].[etl_condition_occurrence_xwalk] x
                          ON x.source_domain = 'OBS_CLIN'
                         AND x.source_record_id = r.source_obsclin_id
                        JOIN [{target_schema}].[condition_occurrence] c
                          ON c.condition_occurrence_id = x.condition_occurrence_id
                        WHERE r.target_domain = 'Condition'
                          AND COALESCE(c.condition_concept_id, 0)
                              <> COALESCE(r.target_concept_id, 0)
                    """)
                ).scalar_one()
            )

            route_concept_zero_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route]
                        WHERE target_domain = 'Condition'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
            )

            target_concept_zero_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[condition_occurrence] c
                        JOIN [{target_schema}].[etl_condition_occurrence_xwalk] x
                          ON x.condition_occurrence_id = c.condition_occurrence_id
                        WHERE x.source_domain = 'OBS_CLIN'
                          AND COALESCE(c.condition_concept_id, 0) = 0
                    """)
                ).scalar_one()
            )

            invalid_standard_target_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route] r
                        LEFT JOIN [{target_schema}].[concept] c
                          ON c.concept_id = r.target_concept_id
                        WHERE r.target_domain = 'Condition'
                          AND COALESCE(r.target_concept_id, 0) <> 0
                          AND (
                               c.concept_id IS NULL
                            OR c.domain_id <> 'Condition'
                            OR c.standard_concept <> 'S'
                            OR c.invalid_reason IS NOT NULL
                          )
                    """)
                ).scalar_one()
            )

            route_target_concepts = [
                {
                    "target_concept_id": int(row[0]),
                    "rows": int(row[1]),
                    "concept_name": None if row[2] is None else str(row[2]),
                    "vocabulary_id": None if row[3] is None else str(row[3]),
                    "concept_code": None if row[4] is None else str(row[4]),
                }
                for row in con.execute(
                    text(f"""
                        SELECT
                            COALESCE(r.target_concept_id, 0),
                            COUNT_BIG(*) AS n,
                            MAX(c.concept_name),
                            MAX(c.vocabulary_id),
                            MAX(c.concept_code)
                        FROM [{target_schema}].[etl_obs_clin_route] r
                        LEFT JOIN [{target_schema}].[concept] c
                          ON c.concept_id = r.target_concept_id
                        WHERE r.target_domain = 'Condition'
                        GROUP BY COALESCE(r.target_concept_id, 0)
                        ORDER BY COUNT_BIG(*) DESC,
                                 COALESCE(r.target_concept_id, 0)
                    """)
                ).fetchall()
            ]

            checks = {
                "route_vs_source": route_rows == source_rows_resolved,
                "route_vs_xwalk": route_rows == xwalk_rows,
                "xwalk_vs_target": xwalk_rows == target_rows,
                "route_source_uniqueness": route_rows == route_distinct_source_rows,
                "xwalk_source_uniqueness": xwalk_rows == xwalk_distinct_source_rows,
                "route_target_match": route_target_mismatch_rows == 0,
                "concept_zero_match": (
                    route_concept_zero_rows == target_concept_zero_rows
                ),
                "standard_condition_semantics": invalid_standard_target_rows == 0,
            }
            status = "matched" if all(checks.values()) else "review_required"

    finally:
        engine.dispose()

    payload: dict[str, object] = {
        "stage": "condition_obs_clin_dynamic_audit",
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
        "route_target_concepts": route_target_concepts,
        "checks": checks,
        "status": status,
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload
