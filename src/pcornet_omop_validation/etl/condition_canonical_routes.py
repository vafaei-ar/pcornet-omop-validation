from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists
from .condition_occurrence import _eligible_ctes


ROUTE_TABLE = "etl_condition_event_route_v2"
EVENT_DOMAINS = (
    "Condition",
    "Observation",
    "Procedure",
    "Measurement",
    "Drug",
    "Device",
    "Specimen",
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _route_cte(source_schema: str, target_schema: str) -> str:
    eligible = _eligible_ctes(source_schema, target_schema)
    event_domain_sql = ",".join(f"'{x}'" for x in EVENT_DOMAINS)
    return eligible + f"""
    , events AS (
      SELECT
        CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
        CAST(DIAGNOSISID AS nvarchar(255)) AS source_record_id,
        CAST(DX AS nvarchar(255)) AS source_code,
        CAST(DX_TYPE AS nvarchar(50)) AS source_code_type,
        CAST(DX_ORIGIN AS nvarchar(50)) AS source_provenance,
        CAST('DX_DATE' AS varchar(32)) AS date_basis,
        vocabulary_id
      FROM diag_eligible

      UNION ALL

      SELECT
        CAST('CONDITION' AS varchar(16)),
        CAST(CONDITIONID AS nvarchar(255)),
        CAST(CONDITION AS nvarchar(255)),
        CAST(CONDITION_TYPE AS nvarchar(50)),
        CAST(CONDITION_SOURCE AS nvarchar(50)),
        CAST(
          CASE WHEN ONSET_DATE IS NOT NULL THEN 'ONSET_DATE' ELSE 'REPORT_DATE' END
          AS varchar(32)
        ),
        vocabulary_id
      FROM cond_eligible
    ),
    source_candidate_counts AS (
      SELECT
        e.source_domain,
        e.source_record_id,
        e.source_code,
        e.source_code_type,
        e.source_provenance,
        e.date_basis,
        e.vocabulary_id,
        COUNT(c.concept_id) AS total_candidates,
        SUM(
          CASE WHEN c.concept_id IS NOT NULL AND c.invalid_reason IS NULL THEN 1 ELSE 0 END
        ) AS active_candidates
      FROM events e
      LEFT JOIN [{target_schema}].[concept] c
        ON c.concept_code = e.source_code
       AND c.vocabulary_id = e.vocabulary_id
      GROUP BY
        e.source_domain, e.source_record_id, e.source_code,
        e.source_code_type, e.source_provenance, e.date_basis, e.vocabulary_id
    ),
    source_resolved AS (
      SELECT
        k.source_domain,
        k.source_record_id,
        k.source_code,
        k.source_code_type,
        k.source_provenance,
        k.date_basis,
        k.vocabulary_id,
        CASE
          WHEN k.active_candidates = 1 THEN active_one.concept_id
          WHEN k.active_candidates = 0 AND k.total_candidates = 1 THEN only_one.concept_id
          ELSE NULL
        END AS source_concept_id,
        k.total_candidates,
        k.active_candidates
      FROM source_candidate_counts k
      OUTER APPLY (
        SELECT MAX(c.concept_id) AS concept_id
        FROM [{target_schema}].[concept] c
        WHERE c.concept_code = k.source_code
          AND c.vocabulary_id = k.vocabulary_id
          AND c.invalid_reason IS NULL
      ) active_one
      OUTER APPLY (
        SELECT MAX(c.concept_id) AS concept_id
        FROM [{target_schema}].[concept] c
        WHERE c.concept_code = k.source_code
          AND c.vocabulary_id = k.vocabulary_id
      ) only_one
    ),
    direct_standard AS (
      SELECT
        s.source_domain,
        s.source_record_id,
        s.source_code,
        s.source_code_type,
        s.source_provenance,
        s.date_basis,
        COALESCE(s.source_concept_id, 0) AS source_concept_id,
        c.domain_id AS target_domain,
        c.concept_id AS target_concept_id,
        CAST('direct_standard_source_concept' AS varchar(64)) AS route_status,
        CAST(NULL AS varchar(32)) AS relationship_id
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
        s.source_code,
        s.source_code_type,
        s.source_provenance,
        s.date_basis,
        COALESCE(s.source_concept_id, 0) AS source_concept_id,
        tgt.domain_id AS target_domain,
        tgt.concept_id AS target_concept_id,
        CAST('maps_to_standard' AS varchar(64)) AS route_status,
        CAST('Maps to' AS varchar(32)) AS relationship_id
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
    nonzero AS (
      SELECT * FROM direct_standard
      UNION ALL
      SELECT * FROM maps_to
    ),
    per_event AS (
      SELECT
        e.source_domain,
        e.source_record_id,
        MAX(CASE WHEN n.target_domain IN ({event_domain_sql}) THEN 1 ELSE 0 END)
          AS has_event_domain_target,
        MAX(CASE WHEN n.target_domain NOT IN ({event_domain_sql}) THEN 1 ELSE 0 END)
          AS has_non_event_domain_target
      FROM events e
      LEFT JOIN nonzero n
        ON n.source_domain = e.source_domain
       AND n.source_record_id = e.source_record_id
      GROUP BY e.source_domain, e.source_record_id
    ),
    unresolved_fallback AS (
      SELECT
        s.source_domain,
        s.source_record_id,
        s.source_code,
        s.source_code_type,
        s.source_provenance,
        s.date_basis,
        COALESCE(s.source_concept_id, 0) AS source_concept_id,
        CAST('Condition' AS varchar(50)) AS target_domain,
        CAST(0 AS bigint) AS target_concept_id,
        CAST(
          CASE
            WHEN p.has_non_event_domain_target = 1
              THEN 'non_event_only_fallback_to_source_semantics'
            WHEN s.source_concept_id IS NULL AND s.active_candidates > 1
              THEN 'ambiguous_active_source_concept'
            WHEN s.source_concept_id IS NULL AND s.total_candidates > 1
              THEN 'ambiguous_inactive_source_concept'
            WHEN s.source_concept_id IS NULL
              THEN 'source_concept_not_found'
            ELSE 'no_active_standard_event_target'
          END AS varchar(64)
        ) AS route_status,
        CAST(NULL AS varchar(32)) AS relationship_id
      FROM source_resolved s
      JOIN per_event p
        ON p.source_domain = s.source_domain
       AND p.source_record_id = s.source_record_id
      WHERE p.has_event_domain_target = 0
    ),
    canonical AS (
      SELECT
        n.source_domain,
        n.source_record_id,
        n.source_code,
        n.source_code_type,
        n.source_provenance,
        n.date_basis,
        n.source_concept_id,
        n.target_domain,
        n.target_concept_id,
        n.route_status,
        n.relationship_id,
        CAST(CASE WHEN n.target_domain IN ({event_domain_sql}) THEN 1 ELSE 0 END AS bit)
          AS is_core_event_route,
        CAST(0 AS bit) AS is_fallback
      FROM nonzero n

      UNION ALL

      SELECT
        f.source_domain,
        f.source_record_id,
        f.source_code,
        f.source_code_type,
        f.source_provenance,
        f.date_basis,
        f.source_concept_id,
        f.target_domain,
        f.target_concept_id,
        f.route_status,
        f.relationship_id,
        CAST(1 AS bit) AS is_core_event_route,
        CAST(1 AS bit) AS is_fallback
      FROM unresolved_fallback f
    )
    """


def materialize_condition_canonical_routes(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "condition_canonical_routes.json"

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

            cte = _route_cte(source_schema, target_schema)
            expected_rows = int(con.execute(text(cte + " SELECT COUNT_BIG(*) FROM canonical")).scalar_one())

            if table_exists(con, target_schema, ROUTE_TABLE):
                existing = int(
                    con.execute(text(f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}]")).scalar_one()
                )
                if existing != expected_rows:
                    raise RuntimeError(
                        f"[{target_schema}].[{ROUTE_TABLE}] has {existing:,} rows but canonical logic expects "
                        f"{expected_rows:,}. Rebuild the isolated validated target rather than patching in place."
                    )
                status = "already_materialized_matched"
            else:
                con.exec_driver_sql(
                    f"""
                    CREATE TABLE [{target_schema}].[{ROUTE_TABLE}] (
                      route_id bigint NOT NULL,
                      source_domain varchar(16) NOT NULL,
                      source_record_id nvarchar(255) NOT NULL,
                      source_code nvarchar(255) NULL,
                      source_code_type nvarchar(50) NULL,
                      source_provenance nvarchar(50) NULL,
                      date_basis varchar(32) NOT NULL,
                      source_concept_id bigint NOT NULL,
                      target_domain varchar(50) NOT NULL,
                      target_concept_id bigint NOT NULL,
                      route_status varchar(64) NOT NULL,
                      relationship_id varchar(32) NULL,
                      is_core_event_route bit NOT NULL,
                      is_fallback bit NOT NULL,
                      CONSTRAINT PK_{ROUTE_TABLE} PRIMARY KEY (route_id),
                      CONSTRAINT UQ_{ROUTE_TABLE}_route UNIQUE
                        (source_domain, source_record_id, target_domain, target_concept_id, route_status)
                    )
                    """
                )
                con.commit()

                con.exec_driver_sql(
                    cte
                    + f"""
                    , numbered AS (
                      SELECT
                        ROW_NUMBER() OVER (
                          ORDER BY source_domain, source_record_id, is_fallback,
                                   target_domain, target_concept_id, route_status
                        ) AS route_id,
                        *
                      FROM canonical
                    )
                    INSERT INTO [{target_schema}].[{ROUTE_TABLE}] (
                      route_id, source_domain, source_record_id, source_code,
                      source_code_type, source_provenance, date_basis,
                      source_concept_id, target_domain, target_concept_id,
                      route_status, relationship_id, is_core_event_route, is_fallback
                    )
                    SELECT
                      route_id, source_domain, source_record_id, source_code,
                      source_code_type, source_provenance, date_basis,
                      source_concept_id, target_domain, target_concept_id,
                      route_status, relationship_id, is_core_event_route, is_fallback
                    FROM numbered
                    """
                )
                con.commit()
                status = "materialized"

            actual_rows = int(
                con.execute(text(f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}]")).scalar_one()
            )
            source_events = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(DISTINCT source_domain + ':' + source_record_id) "
                        f"FROM [{target_schema}].[{ROUTE_TABLE}]"
                    )
                ).scalar_one()
            )
            core_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] "
                        "WHERE is_core_event_route = 1"
                    )
                ).scalar_one()
            )
            fallback_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] WHERE is_fallback = 1"
                    )
                ).scalar_one()
            )
            non_event_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] "
                        "WHERE is_core_event_route = 0"
                    )
                ).scalar_one()
            )
            multi_core_sources = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*) FROM (
                          SELECT source_domain, source_record_id
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE is_core_event_route = 1
                          GROUP BY source_domain, source_record_id
                          HAVING COUNT_BIG(*) > 1
                        ) x
                        """
                    )
                ).scalar_one()
            )
            domain_rows = con.execute(
                text(
                    f"""
                    SELECT target_domain, is_core_event_route, is_fallback,
                           COUNT_BIG(*) AS route_rows,
                           COUNT_BIG(DISTINCT source_domain + ':' + source_record_id) AS source_events
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    GROUP BY target_domain, is_core_event_route, is_fallback
                    ORDER BY is_core_event_route DESC, is_fallback, route_rows DESC, target_domain
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_canonical_routes",
        "status": status,
        "route_table": f"{target_schema}.{ROUTE_TABLE}",
        "source_events": source_events,
        "route_rows": actual_rows,
        "core_event_route_rows": core_rows,
        "non_event_standard_route_rows": non_event_rows,
        "fallback_condition_zero_rows": fallback_rows,
        "multi_core_route_source_events": multi_core_sources,
        "policy": {
            "standard_mapping": "retain every active Standard Maps to target",
            "maps_to_value": "not used as an independent event route",
            "event_domains": list(EVENT_DOMAINS),
            "non_event_targets": (
                "preserve in the canonical routing ledger but do not materialize as clinical event rows"
            ),
            "no_event_domain_target": (
                "retain the source DIAGNOSIS/CONDITION event as Condition concept_id 0"
            ),
            "source_concept_resolution": (
                "use exactly one active exact source concept; if none active use exactly one inactive exact "
                "concept; never choose arbitrarily among multiple candidates"
            ),
        },
        "domains": [
            {
                "target_domain": row[0],
                "is_core_event_route": bool(row[1]),
                "is_fallback": bool(row[2]),
                "route_rows": int(row[3]),
                "source_events": int(row[4]),
            }
            for row in domain_rows
        ],
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"status: {status}")
    print(f"source_events: {source_events}")
    print(f"route_rows: {actual_rows}")
    print(f"core_event_route_rows: {core_rows}")
    print(f"non_event_standard_route_rows: {non_event_rows}")
    print(f"fallback_condition_zero_rows: {fallback_rows}")
    print(f"multi_core_route_source_events: {multi_core_sources}")
    print(f"Audit: {audit_path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize source-derived canonical DIAGNOSIS/CONDITION routing for clean rebuilds."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    materialize_condition_canonical_routes(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
