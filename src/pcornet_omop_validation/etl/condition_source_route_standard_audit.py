from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists
from .condition_occurrence import _eligible_ctes


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def audit_condition_source_routes(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "condition_source_route_standard_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_DIAGNOSIS"),
                (source_schema, "PCORnet_CONDITION"),
                (target_schema, "person"),
                (target_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            eligible = _eligible_ctes(source_schema, target_schema)
            sql = eligible + f"""
            , events AS (
              SELECT
                CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
                CAST(DIAGNOSISID AS nvarchar(255)) AS source_record_id,
                CAST(DX AS nvarchar(255)) AS source_code,
                vocabulary_id
              FROM diag_eligible

              UNION ALL

              SELECT
                CAST('CONDITION' AS varchar(16)),
                CAST(CONDITIONID AS nvarchar(255)),
                CAST(CONDITION AS nvarchar(255)),
                vocabulary_id
              FROM cond_eligible
            ),
            source_candidates AS (
              SELECT
                e.source_domain,
                e.source_record_id,
                e.source_code,
                e.vocabulary_id,
                c.concept_id,
                c.standard_concept,
                c.domain_id,
                c.invalid_reason,
                COUNT(c.concept_id) OVER (
                  PARTITION BY e.source_domain, e.source_record_id
                ) AS candidate_count,
                SUM(CASE WHEN c.concept_id IS NOT NULL AND c.invalid_reason IS NULL THEN 1 ELSE 0 END) OVER (
                  PARTITION BY e.source_domain, e.source_record_id
                ) AS active_candidate_count
              FROM events e
              LEFT JOIN [{target_schema}].[concept] c
                ON c.concept_code = e.source_code
               AND c.vocabulary_id = e.vocabulary_id
            ),
            source_resolved AS (
              SELECT
                source_domain,
                source_record_id,
                source_code,
                vocabulary_id,
                CASE
                  WHEN MAX(active_candidate_count) = 1
                    THEN MAX(CASE WHEN concept_id IS NOT NULL AND invalid_reason IS NULL THEN concept_id END)
                  WHEN MAX(active_candidate_count) = 0 AND MAX(candidate_count) = 1
                    THEN MAX(concept_id)
                  ELSE NULL
                END AS source_concept_id
              FROM source_candidates
              GROUP BY source_domain, source_record_id, source_code, vocabulary_id
            ),
            direct_standard AS (
              SELECT
                s.source_domain,
                s.source_record_id,
                s.source_concept_id,
                c.domain_id AS target_domain,
                c.concept_id AS target_concept_id,
                CAST('direct_standard_source_concept' AS varchar(64)) AS route_status
              FROM source_resolved s
              JOIN [{target_schema}].[concept] c
                ON c.concept_id = s.source_concept_id
               AND c.standard_concept = 'S'
               AND c.invalid_reason IS NULL
            ),
            maps_to AS (
              SELECT DISTINCT
                s.source_domain,
                s.source_record_id,
                s.source_concept_id,
                tgt.domain_id AS target_domain,
                tgt.concept_id AS target_concept_id,
                CAST('maps_to_standard' AS varchar(64)) AS route_status
              FROM source_resolved s
              JOIN [{target_schema}].[concept] src
                ON src.concept_id = s.source_concept_id
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = s.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.invalid_reason IS NULL
              WHERE NOT (
                COALESCE(src.standard_concept, '') = 'S'
                AND src.invalid_reason IS NULL
              )
            ),
            resolved_nonzero AS (
              SELECT * FROM direct_standard
              UNION ALL
              SELECT * FROM maps_to
            ),
            unresolved AS (
              SELECT
                s.source_domain,
                s.source_record_id,
                COALESCE(s.source_concept_id, 0) AS source_concept_id,
                CAST('(unresolved)' AS varchar(50)) AS target_domain,
                CAST(0 AS int) AS target_concept_id,
                CAST(
                  CASE
                    WHEN s.source_concept_id IS NULL THEN 'source_concept_not_unique_or_missing'
                    ELSE 'no_active_standard_target'
                  END AS varchar(64)
                ) AS route_status
              FROM source_resolved s
              WHERE NOT EXISTS (
                SELECT 1
                FROM resolved_nonzero r
                WHERE r.source_domain = s.source_domain
                  AND r.source_record_id = s.source_record_id
              )
            ),
            routes AS (
              SELECT * FROM resolved_nonzero
              UNION ALL
              SELECT * FROM unresolved
            ),
            per_event AS (
              SELECT
                source_domain,
                source_record_id,
                COUNT_BIG(*) AS route_count,
                COUNT_BIG(DISTINCT CASE WHEN target_domain = 'Condition' THEN target_concept_id END) AS condition_target_count,
                COUNT_BIG(DISTINCT CASE WHEN target_domain <> 'Condition' AND target_domain <> '(unresolved)' THEN target_domain END) AS non_condition_domain_count
              FROM routes
              GROUP BY source_domain, source_record_id
            )
            SELECT
              (SELECT COUNT_BIG(*) FROM events) AS source_events,
              (SELECT COUNT_BIG(*) FROM routes) AS route_rows,
              (SELECT COUNT_BIG(*) FROM routes WHERE target_concept_id = 0) AS unresolved_route_rows,
              (SELECT COUNT_BIG(*) FROM per_event WHERE route_count > 1) AS multi_route_source_events,
              (SELECT COALESCE(MAX(route_count), 0) FROM per_event) AS max_routes_per_source,
              (SELECT COUNT_BIG(*) FROM per_event WHERE condition_target_count > 1) AS multi_condition_target_source_events,
              (SELECT COUNT_BIG(*) FROM per_event WHERE non_condition_domain_count > 0) AS cross_domain_source_events
            """
            summary = con.execute(text(sql)).mappings().one()

            domain_sql = eligible + f"""
            , events AS (
              SELECT CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
                     CAST(DIAGNOSISID AS nvarchar(255)) AS source_record_id,
                     CAST(DX AS nvarchar(255)) AS source_code,
                     vocabulary_id
              FROM diag_eligible
              UNION ALL
              SELECT CAST('CONDITION' AS varchar(16)),
                     CAST(CONDITIONID AS nvarchar(255)),
                     CAST(CONDITION AS nvarchar(255)),
                     vocabulary_id
              FROM cond_eligible
            ),
            source_candidates AS (
              SELECT e.*, c.concept_id, c.standard_concept, c.domain_id, c.invalid_reason,
                     COUNT(c.concept_id) OVER (PARTITION BY e.source_domain, e.source_record_id) AS candidate_count,
                     SUM(CASE WHEN c.concept_id IS NOT NULL AND c.invalid_reason IS NULL THEN 1 ELSE 0 END) OVER (PARTITION BY e.source_domain, e.source_record_id) AS active_candidate_count
              FROM events e
              LEFT JOIN [{target_schema}].[concept] c
                ON c.concept_code = e.source_code AND c.vocabulary_id = e.vocabulary_id
            ),
            source_resolved AS (
              SELECT source_domain, source_record_id,
                     CASE
                       WHEN MAX(active_candidate_count) = 1 THEN MAX(CASE WHEN concept_id IS NOT NULL AND invalid_reason IS NULL THEN concept_id END)
                       WHEN MAX(active_candidate_count) = 0 AND MAX(candidate_count) = 1 THEN MAX(concept_id)
                       ELSE NULL
                     END AS source_concept_id
              FROM source_candidates
              GROUP BY source_domain, source_record_id
            ),
            direct_standard AS (
              SELECT s.source_domain, s.source_record_id, c.domain_id AS target_domain, c.concept_id AS target_concept_id
              FROM source_resolved s
              JOIN [{target_schema}].[concept] c
                ON c.concept_id = s.source_concept_id
               AND c.standard_concept = 'S' AND c.invalid_reason IS NULL
            ),
            maps_to AS (
              SELECT DISTINCT s.source_domain, s.source_record_id, tgt.domain_id, tgt.concept_id
              FROM source_resolved s
              JOIN [{target_schema}].[concept] src ON src.concept_id = s.source_concept_id
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = s.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S' AND tgt.invalid_reason IS NULL
              WHERE NOT (
                COALESCE(src.standard_concept, '') = 'S'
                AND src.invalid_reason IS NULL
              )
            ),
            nonzero AS (
              SELECT * FROM direct_standard
              UNION ALL
              SELECT * FROM maps_to
            ),
            routes AS (
              SELECT * FROM nonzero
              UNION ALL
              SELECT s.source_domain, s.source_record_id, CAST('(unresolved)' AS varchar(50)), CAST(0 AS int)
              FROM source_resolved s
              WHERE NOT EXISTS (
                SELECT 1 FROM nonzero n
                WHERE n.source_domain = s.source_domain AND n.source_record_id = s.source_record_id
              )
            )
            SELECT source_domain, target_domain,
                   COUNT_BIG(*) AS route_rows,
                   COUNT_BIG(DISTINCT source_record_id) AS source_events
            FROM routes
            GROUP BY source_domain, target_domain
            ORDER BY source_domain, route_rows DESC, target_domain
            """
            domains = [
                {
                    "source_domain": row[0],
                    "target_domain": row[1],
                    "route_rows": int(row[2]),
                    "source_events": int(row[3]),
                }
                for row in con.execute(text(domain_sql)).fetchall()
            ]
    finally:
        engine.dispose()

    payload = {
        "stage": "condition_source_route_standard_audit",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_events": int(summary["source_events"]),
        "route_rows": int(summary["route_rows"]),
        "one_to_many_expansion": int(summary["route_rows"] - summary["source_events"]),
        "unresolved_route_rows": int(summary["unresolved_route_rows"]),
        "multi_route_source_events": int(summary["multi_route_source_events"]),
        "max_routes_per_source": int(summary["max_routes_per_source"]),
        "multi_condition_target_source_events": int(summary["multi_condition_target_source_events"]),
        "cross_domain_source_events": int(summary["cross_domain_source_events"]),
        "domains": domains,
        "policy_interpretation": (
            "OMOP Standardized Vocabulary Maps to relationships may be one-to-many. "
            "Every active Standard target is an ETL output candidate; unresolved source events "
            "remain represented with concept_id 0."
        ),
        "status": "matched",
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print("status: matched")
    for key in (
        "source_events",
        "route_rows",
        "one_to_many_expansion",
        "unresolved_route_rows",
        "multi_route_source_events",
        "max_routes_per_source",
        "multi_condition_target_source_events",
        "cross_domain_source_events",
    ):
        print(f"{key}: {payload[key]}")
    print("domains:")
    for row in domains:
        print(
            f"  {row['source_domain']} -> {row['target_domain']}: "
            f"routes={row['route_rows']} source_events={row['source_events']}"
        )
    print(f"Audit: {audit_path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit source-derived OMOP one-to-many routing for DIAGNOSIS/CONDITION events."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    audit_condition_source_routes(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
