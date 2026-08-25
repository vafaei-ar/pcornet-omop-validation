from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists
from .condition_occurrence import _eligible_ctes


EVENT_DOMAINS = {
    "Condition",
    "Observation",
    "Procedure",
    "Measurement",
    "Drug",
    "Device",
    "Specimen",
}


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _materialize_temp_routes(connection, source_schema: str, target_schema: str) -> None:
    """Materialize the expensive routing logic once in SQL Server temp tables."""
    eligible = _eligible_ctes(source_schema, target_schema)

    connection.exec_driver_sql(
        """
        IF OBJECT_ID('tempdb..#events') IS NOT NULL DROP TABLE #events;
        IF OBJECT_ID('tempdb..#code_keys') IS NOT NULL DROP TABLE #code_keys;
        IF OBJECT_ID('tempdb..#code_map') IS NOT NULL DROP TABLE #code_map;
        IF OBJECT_ID('tempdb..#source_resolved') IS NOT NULL DROP TABLE #source_resolved;
        IF OBJECT_ID('tempdb..#nonzero') IS NOT NULL DROP TABLE #nonzero;
        IF OBJECT_ID('tempdb..#per_event') IS NOT NULL DROP TABLE #per_event;
        """
    )

    connection.exec_driver_sql(
        eligible
        + """
        SELECT
          CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
          CAST(DIAGNOSISID AS nvarchar(255)) AS source_record_id,
          CAST(DX AS nvarchar(255)) AS source_code,
          vocabulary_id
        INTO #events
        FROM diag_eligible

        UNION ALL

        SELECT
          CAST('CONDITION' AS varchar(16)),
          CAST(CONDITIONID AS nvarchar(255)),
          CAST(CONDITION AS nvarchar(255)),
          vocabulary_id
        FROM cond_eligible;
        """
    )
    connection.exec_driver_sql(
        "CREATE CLUSTERED INDEX IX_events_record ON #events(source_domain, source_record_id);"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IX_events_code ON #events(vocabulary_id, source_code);"
    )

    connection.exec_driver_sql(
        """
        SELECT DISTINCT source_code, vocabulary_id
        INTO #code_keys
        FROM #events;
        CREATE UNIQUE CLUSTERED INDEX IX_code_keys
          ON #code_keys(vocabulary_id, source_code);
        """
    )

    connection.exec_driver_sql(
        f"""
        SELECT
          k.source_code,
          k.vocabulary_id,
          CASE
            WHEN SUM(CASE WHEN c.concept_id IS NOT NULL AND c.invalid_reason IS NULL THEN 1 ELSE 0 END) = 1
              THEN MAX(CASE WHEN c.invalid_reason IS NULL THEN c.concept_id END)
            WHEN COUNT(c.concept_id) = 1
             AND SUM(CASE WHEN c.concept_id IS NOT NULL AND c.invalid_reason IS NULL THEN 1 ELSE 0 END) = 0
              THEN MAX(c.concept_id)
            ELSE NULL
          END AS source_concept_id
        INTO #code_map
        FROM #code_keys k
        LEFT JOIN [{target_schema}].[concept] c
          ON c.concept_code = k.source_code
         AND c.vocabulary_id = k.vocabulary_id
        GROUP BY k.source_code, k.vocabulary_id;

        CREATE UNIQUE CLUSTERED INDEX IX_code_map
          ON #code_map(vocabulary_id, source_code);
        CREATE INDEX IX_code_map_concept ON #code_map(source_concept_id);
        """
    )

    connection.exec_driver_sql(
        """
        SELECT
          e.source_domain,
          e.source_record_id,
          e.source_code,
          e.vocabulary_id,
          m.source_concept_id
        INTO #source_resolved
        FROM #events e
        LEFT JOIN #code_map m
          ON m.vocabulary_id = e.vocabulary_id
         AND m.source_code = e.source_code;

        CREATE CLUSTERED INDEX IX_source_resolved_record
          ON #source_resolved(source_domain, source_record_id);
        CREATE INDEX IX_source_resolved_concept
          ON #source_resolved(source_concept_id);
        """
    )

    connection.exec_driver_sql(
        f"""
        SELECT
          s.source_domain,
          s.source_record_id,
          s.source_code,
          s.vocabulary_id,
          s.source_concept_id,
          c.domain_id AS target_domain,
          c.concept_id AS target_concept_id,
          CAST('direct_standard_source_concept' AS varchar(64)) AS route_status,
          CAST(NULL AS varchar(32)) AS relationship_id
        INTO #nonzero
        FROM #source_resolved s
        JOIN [{target_schema}].[concept] c
          ON c.concept_id = s.source_concept_id
         AND c.standard_concept = 'S'
         AND c.invalid_reason IS NULL;

        INSERT INTO #nonzero (
          source_domain, source_record_id, source_code, vocabulary_id,
          source_concept_id, target_domain, target_concept_id,
          route_status, relationship_id
        )
        SELECT DISTINCT
          s.source_domain,
          s.source_record_id,
          s.source_code,
          s.vocabulary_id,
          s.source_concept_id,
          tgt.domain_id,
          tgt.concept_id,
          CAST('maps_to_standard' AS varchar(64)),
          CAST(cr.relationship_id AS varchar(32))
        FROM #source_resolved s
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
        );

        CREATE CLUSTERED INDEX IX_nonzero_record
          ON #nonzero(source_domain, source_record_id);
        CREATE INDEX IX_nonzero_domain
          ON #nonzero(target_domain, route_status);
        """
    )

    connection.exec_driver_sql(
        """
        SELECT
          source_domain,
          source_record_id,
          MAX(CASE WHEN target_domain IN
            ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen')
            THEN 1 ELSE 0 END) AS has_event_domain_target,
          MAX(CASE WHEN target_domain NOT IN
            ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen')
            THEN 1 ELSE 0 END) AS has_non_event_domain_target
        INTO #per_event
        FROM #nonzero
        GROUP BY source_domain, source_record_id;

        CREATE UNIQUE CLUSTERED INDEX IX_per_event_record
          ON #per_event(source_domain, source_record_id);
        """
    )


def audit_condition_route_domain_semantics(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "condition_route_domain_semantics_audit.json"

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
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            _materialize_temp_routes(con, source_schema, target_schema)

            domain_rows = con.execute(
                text(
                    """
                    SELECT
                      n.source_domain,
                      n.target_domain,
                      n.route_status,
                      COUNT_BIG(*) AS route_rows,
                      COUNT_BIG(DISTINCT n.source_record_id) AS source_events,
                      SUM(CASE WHEN p.has_event_domain_target = 1 THEN 1 ELSE 0 END)
                        AS rows_on_events_with_event_domain_target
                    FROM #nonzero n
                    JOIN #per_event p
                      ON p.source_domain = n.source_domain
                     AND p.source_record_id = n.source_record_id
                    GROUP BY n.source_domain, n.target_domain, n.route_status
                    ORDER BY n.source_domain, route_rows DESC,
                             n.target_domain, n.route_status
                    """
                )
            ).fetchall()

            unusual_rows = con.execute(
                text(
                    """
                    SELECT
                      n.source_domain,
                      n.target_domain,
                      n.route_status,
                      COUNT_BIG(*) AS route_rows,
                      COUNT_BIG(DISTINCT n.source_record_id) AS source_events,
                      SUM(CASE WHEN p.has_event_domain_target = 1 THEN 1 ELSE 0 END)
                        AS rows_with_event_domain_companion,
                      SUM(CASE WHEN p.has_event_domain_target = 0 THEN 1 ELSE 0 END)
                        AS rows_without_event_domain_companion
                    FROM #nonzero n
                    JOIN #per_event p
                      ON p.source_domain = n.source_domain
                     AND p.source_record_id = n.source_record_id
                    WHERE n.target_domain NOT IN
                      ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen')
                    GROUP BY n.source_domain, n.target_domain, n.route_status
                    ORDER BY n.source_domain, route_rows DESC, n.target_domain
                    """
                )
            ).fetchall()

            relationship_rows = con.execute(
                text(
                    f"""
                    SELECT
                      s.source_domain,
                      tgt.domain_id AS target_domain,
                      cr.relationship_id,
                      COUNT_BIG(*) AS relationship_rows,
                      COUNT_BIG(DISTINCT s.source_record_id) AS source_events
                    FROM #source_resolved s
                    JOIN [{target_schema}].[concept_relationship] cr
                      ON cr.concept_id_1 = s.source_concept_id
                     AND cr.relationship_id IN ('Maps to', 'Maps to value')
                     AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                    JOIN [{target_schema}].[concept] tgt
                      ON tgt.concept_id = cr.concept_id_2
                     AND tgt.invalid_reason IS NULL
                    GROUP BY s.source_domain, tgt.domain_id, cr.relationship_id
                    ORDER BY s.source_domain, cr.relationship_id,
                             relationship_rows DESC, tgt.domain_id
                    """
                )
            ).fetchall()

            totals = con.execute(
                text(
                    """
                    SELECT
                      COUNT_BIG(*) AS nonzero_route_rows,
                      COUNT_BIG(DISTINCT CASE WHEN n.target_domain IN
                        ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen')
                        THEN n.source_domain + ':' + n.source_record_id END)
                        AS source_events_with_event_domain_target,
                      COUNT_BIG(DISTINCT CASE WHEN n.target_domain NOT IN
                        ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen')
                        THEN n.source_domain + ':' + n.source_record_id END)
                        AS source_events_with_non_event_domain_target,
                      COUNT_BIG(DISTINCT CASE
                        WHEN p.has_non_event_domain_target = 1
                         AND p.has_event_domain_target = 0
                        THEN n.source_domain + ':' + n.source_record_id END)
                        AS source_events_only_non_event_domains
                    FROM #nonzero n
                    JOIN #per_event p
                      ON p.source_domain = n.source_domain
                     AND p.source_record_id = n.source_record_id
                    """
                )
            ).mappings().one()
    finally:
        engine.dispose()

    payload = {
        "stage": "condition_route_domain_semantics_audit",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "event_domains": sorted(EVENT_DOMAINS),
        "totals": {key: int(value or 0) for key, value in totals.items()},
        "routes_by_domain_and_basis": [
            {
                "source_domain": row[0],
                "target_domain": row[1],
                "route_status": row[2],
                "route_rows": int(row[3]),
                "source_events": int(row[4]),
                "rows_on_events_with_event_domain_target": int(row[5] or 0),
            }
            for row in domain_rows
        ],
        "non_event_domain_routes": [
            {
                "source_domain": row[0],
                "target_domain": row[1],
                "route_status": row[2],
                "route_rows": int(row[3]),
                "source_events": int(row[4]),
                "rows_with_event_domain_companion": int(row[5] or 0),
                "rows_without_event_domain_companion": int(row[6] or 0),
            }
            for row in unusual_rows
        ],
        "maps_to_and_maps_to_value_profile": [
            {
                "source_domain": row[0],
                "target_domain": row[1],
                "relationship_id": row[2],
                "relationship_rows": int(row[3]),
                "source_events": int(row[4]),
            }
            for row in relationship_rows
        ],
        "interpretation": (
            "Event-domain Standard targets are candidates for OMOP clinical event tables. "
            "Non-event domains such as Meas Value, Unit, Relationship, and Spec Anatomic Site "
            "require separate semantic treatment and must not be blindly materialized as clinical events."
        ),
        "implementation_note": (
            "The audit materializes source-event resolution once into session-scoped SQL Server "
            "temporary tables and indexes them before computing summaries. No persistent OMOP or "
            "PCORnet table is modified."
        ),
        "status": "matched",
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("status: matched")
    for key, value in payload["totals"].items():
        print(f"{key}: {value}")
    print("non-event domain routes:")
    for row in payload["non_event_domain_routes"]:
        print(
            f"  {row['source_domain']} -> {row['target_domain']} "
            f"basis={row['route_status']} routes={row['route_rows']} "
            f"events={row['source_events']} "
            f"companion_event_rows={row['rows_with_event_domain_companion']} "
            f"no_companion_rows={row['rows_without_event_domain_companion']}"
        )
    print("Maps to / Maps to value profile:")
    for row in payload["maps_to_and_maps_to_value_profile"]:
        print(
            f"  {row['source_domain']} -> {row['target_domain']} "
            f"{row['relationship_id']}: rows={row['relationship_rows']} "
            f"events={row['source_events']}"
        )
    print(f"Audit: {audit_path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit semantic treatment of cross-domain DIAGNOSIS/CONDITION "
            "vocabulary routes."
        )
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    audit_condition_route_domain_semantics(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
