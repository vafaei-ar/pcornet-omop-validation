from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


DOMAINS = ("Condition", "Device", "Specimen")

EXPECTED_ROUTE_ROWS = {
    "Condition": 1_210,
    "Device": 196_230,
    "Specimen": 47,
}


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def audit_procedure_remaining_routes(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    route_table = "etl_procedure_event_route"
    audit_path = config.audit_dir / "procedure_remaining_routes.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            required = (
                (source_schema, route_table),
                (source_schema, "PCORnet_PROCEDURES"),
                (target_schema, "person"),
                (source_schema, "etl_visit_occurrence_xwalk"),
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
            for domain in DOMAINS:
                route_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{route_table}]
                    WHERE target_domain = :domain
                    """,
                    {"domain": domain},
                )
                distinct_source_events = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(DISTINCT source_procedure_id)
                    FROM [{source_schema}].[{route_table}]
                    WHERE target_domain = :domain
                    """,
                    {"domain": domain},
                )
                concept_zero_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{route_table}]
                    WHERE target_domain = :domain
                      AND target_concept_id = 0
                    """,
                    {"domain": domain},
                )
                event_route_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{route_table}]
                    WHERE target_domain = :domain
                      AND disposition = 'event_route'
                    """,
                    {"domain": domain},
                )
                unresolved_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{route_table}]
                    WHERE target_domain = :domain
                      AND disposition = 'unresolved'
                    """,
                    {"domain": domain},
                )
                person_unlinked_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{route_table}] r
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
                    FROM [{source_schema}].[{route_table}] r
                    JOIN [{source_schema}].[etl_visit_occurrence_xwalk] v
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
                        FROM [{source_schema}].[{route_table}]
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
                        FROM [{source_schema}].[{route_table}]
                        WHERE target_domain = :domain
                        GROUP BY source_procedure_id
                        HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                    {"domain": domain},
                )

                expected = EXPECTED_ROUTE_ROWS[domain]
                if route_rows != expected:
                    raise RuntimeError(
                        f"{domain} route count changed: {route_rows:,} != {expected:,}"
                    )

                domains[domain] = {
                    "route_rows": route_rows,
                    "distinct_source_events": distinct_source_events,
                    "one_to_many_expansion": route_rows - distinct_source_events,
                    "multi_source_events": multi_source_events,
                    "max_routes_per_source": max_routes_per_source,
                    "event_route_rows": event_route_rows,
                    "unresolved_rows": unresolved_rows,
                    "concept_zero_rows": concept_zero_rows,
                    "person_unlinked_rows": person_unlinked_rows,
                    "visit_linked_rows": visit_linked_rows,
                }

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

            condition_xwalk = None
            if table_exists(connection, source_schema, "etl_condition_occurrence_xwalk"):
                condition_xwalk = {
                    "rows": _scalar(
                        connection,
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[etl_condition_occurrence_xwalk]
                        """,
                    ),
                    "procedure_rows": _scalar(
                        connection,
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[etl_condition_occurrence_xwalk]
                        WHERE source_domain = 'PROCEDURES'
                        """,
                    ),
                }

            type_candidates = [
                {
                    "concept_id": int(row[0]),
                    "concept_name": str(row[1]),
                    "domain_id": str(row[2]),
                    "vocabulary_id": str(row[3]),
                    "concept_class_id": str(row[4]),
                    "standard_concept": row[5],
                    "invalid_reason": row[6],
                }
                for row in connection.execute(
                    text(
                        f"""
                        SELECT TOP (100)
                            concept_id,
                            concept_name,
                            domain_id,
                            vocabulary_id,
                            concept_class_id,
                            standard_concept,
                            invalid_reason
                        FROM [{target_schema}].[concept]
                        WHERE invalid_reason IS NULL
                          AND (
                               domain_id = 'Type Concept'
                            OR vocabulary_id = 'Type Concept'
                          )
                          AND (
                               concept_name LIKE '%EHR%'
                            OR concept_name LIKE '%procedure%'
                            OR concept_name LIKE '%device%'
                            OR concept_name LIKE '%specimen%'
                          )
                        ORDER BY concept_id
                        """
                    )
                ).fetchall()
            ]

            target_columns: dict[str, list[dict[str, object]]] = {}
            for table in ("condition_occurrence", "device_exposure", "specimen"):
                target_columns[table] = [
                    {
                        "column_name": str(row[0]),
                        "data_type": str(row[1]),
                        "max_length": int(row[2]) if row[2] is not None else None,
                        "is_nullable": bool(row[3]),
                    }
                    for row in connection.execute(
                        text(
                            """
                            SELECT
                                c.name,
                                t.name,
                                CASE
                                    WHEN c.max_length < 0 THEN NULL
                                    ELSE c.max_length
                                END,
                                c.is_nullable
                            FROM sys.columns c
                            JOIN sys.types t
                              ON t.user_type_id = c.user_type_id
                            WHERE c.object_id = OBJECT_ID(:obj)
                            ORDER BY c.column_id
                            """
                        ),
                        {"obj": f"{target_schema}.{table}"},
                    ).fetchall()
                ]

        payload = {
            "stage": "procedure_remaining_routes",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "domains": domains,
            "target_counts": target_counts,
            "condition_xwalk": condition_xwalk,
            "type_concept_candidates": type_candidates,
            "target_columns": target_columns,
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
