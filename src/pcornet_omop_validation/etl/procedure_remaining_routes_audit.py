from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


DOMAINS = ("Condition", "Device", "Specimen")
ROUTE_TABLE = "etl_procedure_event_route"
CONDITION_XWALK = "etl_condition_occurrence_xwalk"
DEVICE_XWALK = "etl_device_exposure_xwalk"
SPECIMEN_XWALK = "etl_specimen_xwalk"


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def audit_procedure_remaining_routes(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_remaining_routes.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            required = (
                (source_schema, "PCORnet_PROCEDURES"),
                (target_schema, ROUTE_TABLE),
                (target_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "person"),
                (target_schema, "condition_occurrence"),
                (target_schema, "device_exposure"),
                (target_schema, "specimen"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            domains: dict[str, dict[str, object]] = {}
            checks: dict[str, bool] = {}
            for domain in DOMAINS:
                route_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE target_domain = :domain
                    """,
                    {"domain": domain},
                )
                distinct_source_events = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(DISTINCT source_procedure_id)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE target_domain = :domain
                    """,
                    {"domain": domain},
                )
                concept_zero_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE target_domain = :domain
                      AND target_concept_id = 0
                    """,
                    {"domain": domain},
                )
                invalid_standard_target_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = r.target_concept_id
                    WHERE r.target_domain = :domain
                      AND r.target_concept_id <> 0
                      AND (
                           c.concept_id IS NULL
                        OR c.standard_concept <> 'S'
                        OR c.invalid_reason IS NOT NULL
                        OR c.domain_id <> :domain
                      )
                    """,
                    {"domain": domain},
                )
                person_unlinked_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    LEFT JOIN [{target_schema}].[person] p
                      ON p.person_source_value = r.patid
                    WHERE r.target_domain = :domain
                      AND p.person_id IS NULL
                    """,
                    {"domain": domain},
                )
                visit_linked_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                      ON v.encounterid = r.encounterid
                    WHERE r.target_domain = :domain
                    """,
                    {"domain": domain},
                )
                max_routes_per_source = _scalar(
                    connection,
                    f"""
                    SELECT COALESCE(MAX(route_count), 0)
                    FROM (
                      SELECT source_procedure_id, COUNT_BIG(*) AS route_count
                      FROM [{target_schema}].[{ROUTE_TABLE}]
                      WHERE target_domain = :domain
                      GROUP BY source_procedure_id
                    ) x
                    """,
                    {"domain": domain},
                )
                multi_source_events = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM (
                      SELECT source_procedure_id
                      FROM [{target_schema}].[{ROUTE_TABLE}]
                      WHERE target_domain = :domain
                      GROUP BY source_procedure_id
                      HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                    {"domain": domain},
                )
                domains[domain] = {
                    "route_rows": route_rows,
                    "distinct_source_events": distinct_source_events,
                    "one_to_many_expansion": route_rows - distinct_source_events,
                    "multi_source_events": multi_source_events,
                    "max_routes_per_source": max_routes_per_source,
                    "concept_zero_rows": concept_zero_rows,
                    "invalid_standard_target_rows": invalid_standard_target_rows,
                    "person_unlinked_rows": person_unlinked_rows,
                    "visit_linked_rows": visit_linked_rows,
                }
                checks[f"{domain}_standard_semantics"] = (
                    invalid_standard_target_rows == 0
                )
                checks[f"{domain}_person_linkage"] = person_unlinked_rows == 0
                checks[f"{domain}_one_route_per_source"] = multi_source_events == 0

            target_counts = {
                "condition_occurrence": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
                ),
                "device_exposure": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure]",
                ),
                "specimen": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen]",
                ),
            }

            lineage = {
                "condition_xwalk_exists": table_exists(
                    connection, target_schema, CONDITION_XWALK
                ),
                "device_xwalk_exists": table_exists(
                    connection, target_schema, DEVICE_XWALK
                ),
                "specimen_xwalk_exists": table_exists(
                    connection, target_schema, SPECIMEN_XWALK
                ),
            }
            if lineage["condition_xwalk_exists"]:
                lineage["condition_procedure_rows"] = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{CONDITION_XWALK}]
                    WHERE source_domain = 'PROCEDURES'
                    """,
                )
            if lineage["device_xwalk_exists"]:
                lineage["device_rows"] = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{DEVICE_XWALK}]",
                )
            if lineage["specimen_xwalk_exists"]:
                lineage["specimen_rows"] = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{SPECIMEN_XWALK}]",
                )

            materialized_type_counts = {
                "Condition": (
                    [
                        {"type_concept_id": int(row[0]), "rows": int(row[1])}
                        for row in connection.execute(
                            text(
                                f"""
                                SELECT c.condition_type_concept_id, COUNT_BIG(*)
                                FROM [{target_schema}].[condition_occurrence] c
                                JOIN [{target_schema}].[{CONDITION_XWALK}] x
                                  ON x.condition_occurrence_id = c.condition_occurrence_id
                                WHERE x.source_domain = 'PROCEDURES'
                                GROUP BY c.condition_type_concept_id
                                ORDER BY COUNT_BIG(*) DESC
                                """
                            )
                        ).fetchall()
                    ]
                    if lineage["condition_xwalk_exists"]
                    else []
                ),
                "Device": [
                    {"type_concept_id": int(row[0]), "rows": int(row[1])}
                    for row in connection.execute(
                        text(
                            f"""
                            SELECT device_type_concept_id, COUNT_BIG(*)
                            FROM [{target_schema}].[device_exposure]
                            GROUP BY device_type_concept_id
                            ORDER BY COUNT_BIG(*) DESC
                            """
                        )
                    ).fetchall()
                ],
                "Specimen": [
                    {"type_concept_id": int(row[0]), "rows": int(row[1])}
                    for row in connection.execute(
                        text(
                            f"""
                            SELECT specimen_type_concept_id, COUNT_BIG(*)
                            FROM [{target_schema}].[specimen]
                            GROUP BY specimen_type_concept_id
                            ORDER BY COUNT_BIG(*) DESC
                            """
                        )
                    ).fetchall()
                ],
            }

        status = "matched" if all(checks.values()) else "review_required"
        payload = {
            "stage": "procedure_remaining_routes",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "domains": domains,
            "target_counts": target_counts,
            "lineage": lineage,
            "materialized_type_counts": materialized_type_counts,
            "checks": checks,
            "status": status,
            "interpretation_note": (
                "Route counts are descriptive and source-derived. Nonzero targets "
                "must be active Standard concepts in the declared domain. Current "
                "materialized type-concept distributions are reported separately; "
                "the validated primary policy does not infer an OMOP type concept "
                "from PROCEDURES table membership alone."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
