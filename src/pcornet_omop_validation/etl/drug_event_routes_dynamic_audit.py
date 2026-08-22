from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_drug_event_route"
PROCEDURE_ROUTE_TABLE = "etl_procedure_event_route"

SOURCE_KEYS = {
    "PRESCRIBING": ("PCORnet_PRESCRIBING", "PRESCRIBINGID"),
    "DISPENSING": ("PCORnet_DISPENSING", "DISPENSINGID"),
    "MED_ADMIN": ("PCORnet_MED_ADMIN", "MEDADMINID"),
    "IMMUNIZATION": ("PCORnet_IMMUNIZATION", "IMMUNIZATIONID"),
}


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def audit_drug_event_routes_dynamic(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "drug_event_routes_dynamic_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required_source = [table for table, _ in SOURCE_KEYS.values()]
            required_target = [ROUTE_TABLE, PROCEDURE_ROUTE_TABLE, "concept"]

            for table in required_source:
                if not table_exists(con, source_schema, table):
                    raise RuntimeError(
                        f"Required table [{source_schema}].[{table}] does not exist"
                    )
            for table in required_target:
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(
                        f"Required table [{target_schema}].[{table}] does not exist"
                    )

            source_events: dict[str, int] = {}
            source_key_issues: dict[str, dict[str, int]] = {}
            for family, (table, key) in SOURCE_KEYS.items():
                source_events[family] = _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}]",
                )
                missing_key = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[{table}]
                    WHERE [{key}] IS NULL
                       OR LTRIM(RTRIM(CONVERT(nvarchar(255), [{key}]))) = ''
                    """,
                )
                duplicate_groups = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM (
                        SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), [{key}]))) AS k
                        FROM [{source_schema}].[{table}]
                        WHERE [{key}] IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), [{key}]))) <> ''
                        GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), [{key}])))
                        HAVING COUNT_BIG(*) > 1
                    ) d
                    """,
                )
                source_key_issues[family] = {
                    "missing_key_rows": missing_key,
                    "duplicate_key_groups": duplicate_groups,
                }

            procedure_source_events = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(DISTINCT source_procedure_id)
                FROM [{target_schema}].[{PROCEDURE_ROUTE_TABLE}]
                WHERE target_domain = 'Drug'
                """,
            )
            source_events["PROCEDURES"] = procedure_source_events

            families = tuple(source_events)
            family_metrics: dict[str, dict[str, int]] = {}
            for family in families:
                route_rows = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE source_domain = :family
                    """,
                    {"family": family},
                )
                distinct_sources = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(DISTINCT source_record_id)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE source_domain = :family
                    """,
                    {"family": family},
                )
                unresolved = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE source_domain = :family
                      AND COALESCE(target_concept_id, 0) = 0
                    """,
                    {"family": family},
                )
                multi_source_events = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM (
                        SELECT source_record_id
                        FROM [{target_schema}].[{ROUTE_TABLE}]
                        WHERE source_domain = :family
                        GROUP BY source_record_id
                        HAVING COUNT_BIG(*) > 1
                    ) q
                    """,
                    {"family": family},
                )
                max_routes = _scalar(
                    con,
                    f"""
                    SELECT COALESCE(MAX(n), 0)
                    FROM (
                        SELECT source_record_id, COUNT_BIG(*) AS n
                        FROM [{target_schema}].[{ROUTE_TABLE}]
                        WHERE source_domain = :family
                        GROUP BY source_record_id
                    ) q
                    """,
                    {"family": family},
                )
                invalid_targets = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = r.target_concept_id
                    WHERE r.source_domain = :family
                      AND COALESCE(r.target_concept_id, 0) <> 0
                      AND (
                           c.concept_id IS NULL
                        OR c.domain_id <> 'Drug'
                        OR c.standard_concept <> 'S'
                        OR c.invalid_reason IS NOT NULL
                      )
                    """,
                    {"family": family},
                )
                family_metrics[family] = {
                    "source_events": source_events[family],
                    "route_rows": route_rows,
                    "distinct_source_events": distinct_sources,
                    "one_to_many_expansion": route_rows - distinct_sources,
                    "multi_source_events": multi_source_events,
                    "max_routes_per_source": max_routes,
                    "concept_zero_rows": unresolved,
                    "invalid_standard_target_rows": invalid_targets,
                }

            orphan_routes = 0
            for family, (table, key) in SOURCE_KEYS.items():
                orphan_routes += _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    LEFT JOIN [{source_schema}].[{table}] s
                      ON r.source_record_id = LTRIM(RTRIM(CONVERT(nvarchar(255), s.[{key}])))
                    WHERE r.source_domain = :family
                      AND s.[{key}] IS NULL
                    """,
                    {"family": family},
                )

            orphan_routes += _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[{PROCEDURE_ROUTE_TABLE}] p
                  ON p.source_procedure_id = r.source_record_id
                 AND p.target_domain = 'Drug'
                WHERE r.source_domain = 'PROCEDURES'
                  AND p.source_procedure_id IS NULL
                """,
            )

            route_total = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}]",
            )
            distinct_total = sum(m["distinct_source_events"] for m in family_metrics.values())
            source_total = sum(source_events.values())
            concept_zero_total = sum(m["concept_zero_rows"] for m in family_metrics.values())
            invalid_target_total = sum(m["invalid_standard_target_rows"] for m in family_metrics.values())

            checks = {
                "source_keys_unique_complete": all(
                    x["missing_key_rows"] == 0 and x["duplicate_key_groups"] == 0
                    for x in source_key_issues.values()
                ),
                "route_source_coverage": all(
                    family_metrics[f]["distinct_source_events"] == source_events[f]
                    for f in families
                ),
                "route_sources_resolve": orphan_routes == 0,
                "standard_drug_target_semantics": invalid_target_total == 0,
                "route_total_reconciles": route_total == sum(
                    m["route_rows"] for m in family_metrics.values()
                ),
                "distinct_total_reconciles": distinct_total == source_total,
            }
            status = "matched" if all(checks.values()) else "review_required"

        payload = {
            "stage": "drug_event_routes_dynamic_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "source_events": source_events,
            "source_key_issues": source_key_issues,
            "families": family_metrics,
            "source_event_total": source_total,
            "route_rows": route_total,
            "one_to_many_expansion": route_total - source_total,
            "concept_zero_rows": concept_zero_total,
            "orphan_route_rows": orphan_routes,
            "invalid_standard_target_rows": invalid_target_total,
            "checks": checks,
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()


if __name__ == "__main__":
    import argparse

    from .config import load_etl_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    result = audit_drug_event_routes_dynamic(load_etl_config(args.config))
    print(f"status: {result['status']}")
    for family, metrics in result["families"].items():
        print(
            f"{family}: source={metrics['source_events']} "
            f"routes={metrics['route_rows']} "
            f"distinct={metrics['distinct_source_events']} "
            f"expansion={metrics['one_to_many_expansion']} "
            f"concept0={metrics['concept_zero_rows']} "
            f"multi_sources={metrics['multi_source_events']} "
            f"max_routes={metrics['max_routes_per_source']} "
            f"invalid_targets={metrics['invalid_standard_target_rows']}"
        )
    print(f"source_event_total: {result['source_event_total']}")
    print(f"route_rows: {result['route_rows']}")
    print(f"one_to_many_expansion: {result['one_to_many_expansion']}")
    print(f"concept_zero_rows: {result['concept_zero_rows']}")
    print(f"orphan_route_rows: {result['orphan_route_rows']}")
    print(f"invalid_standard_target_rows: {result['invalid_standard_target_rows']}")
    print("checks:")
    for key, value in result["checks"].items():
        print(f"  {key}: {value}")
    print(f"Audit: {result['audit_path']}")
