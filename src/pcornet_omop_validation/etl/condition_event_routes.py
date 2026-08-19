from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_condition_event_route"


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "etl_condition_occurrence_xwalk"),
        (target_schema, "condition_occurrence"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _routing_cte(source_schema: str, target_schema: str) -> str:
    return f"""
    WITH base AS (
      SELECT
        x.source_domain,
        x.source_record_id,
        x.source_code_type,
        x.source_provenance,
        x.date_basis,
        co.condition_occurrence_id AS source_condition_occurrence_id,
        co.condition_source_concept_id AS source_concept_id,
        co.condition_concept_id,
        src.domain_id AS source_concept_domain,
        src.standard_concept AS source_standard_concept,
        src.invalid_reason AS source_invalid_reason
      FROM [{target_schema}].[condition_occurrence] co
      JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
        ON x.condition_occurrence_id = co.condition_occurrence_id
      LEFT JOIN [{target_schema}].[concept] src
        ON src.concept_id = co.condition_source_concept_id
    ),
    mapped_targets AS (
      SELECT DISTINCT
        b.source_domain,
        b.source_record_id,
        b.source_code_type,
        b.source_provenance,
        b.date_basis,
        b.source_condition_occurrence_id,
        b.source_concept_id,
        tgt.domain_id AS target_domain,
        tgt.concept_id AS target_concept_id,
        CAST('maps_to_standard' AS varchar(64)) AS route_status
      FROM base b
      JOIN [{target_schema}].[concept_relationship] cr
        ON cr.concept_id_1 = b.source_concept_id
       AND cr.relationship_id = 'Maps to'
       AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
      JOIN [{target_schema}].[concept] tgt
        ON tgt.concept_id = cr.concept_id_2
       AND tgt.standard_concept = 'S'
       AND tgt.invalid_reason IS NULL
      WHERE b.condition_concept_id = 0
        AND NOT (
          b.source_concept_id <> 0
          AND b.source_invalid_reason IS NULL
          AND b.source_standard_concept = 'S'
        )
    ),
    resolved AS (
      SELECT
        b.source_domain,
        b.source_record_id,
        b.source_code_type,
        b.source_provenance,
        b.date_basis,
        b.source_condition_occurrence_id,
        b.source_concept_id,
        CAST('Condition' AS varchar(50)) AS target_domain,
        b.condition_concept_id AS target_concept_id,
        CAST('existing_condition_mapping' AS varchar(64)) AS route_status
      FROM base b
      WHERE b.condition_concept_id <> 0

      UNION ALL

      SELECT
        b.source_domain,
        b.source_record_id,
        b.source_code_type,
        b.source_provenance,
        b.date_basis,
        b.source_condition_occurrence_id,
        b.source_concept_id,
        CAST(b.source_concept_domain AS varchar(50)) AS target_domain,
        b.source_concept_id AS target_concept_id,
        CAST('direct_standard_source_concept' AS varchar(64)) AS route_status
      FROM base b
      WHERE b.condition_concept_id = 0
        AND b.source_concept_id <> 0
        AND b.source_invalid_reason IS NULL
        AND b.source_standard_concept = 'S'

      UNION ALL

      SELECT * FROM mapped_targets

      UNION ALL

      SELECT
        b.source_domain,
        b.source_record_id,
        b.source_code_type,
        b.source_provenance,
        b.date_basis,
        b.source_condition_occurrence_id,
        b.source_concept_id,
        CAST('(unresolved)' AS varchar(50)) AS target_domain,
        CAST(0 AS bigint) AS target_concept_id,
        CAST(
          CASE
            WHEN b.source_concept_id = 0 THEN 'source_concept_not_found'
            WHEN b.source_invalid_reason IS NOT NULL THEN 'invalid_source_without_standard_target'
            ELSE 'no_active_standard_target'
          END
          AS varchar(64)
        ) AS route_status
      FROM base b
      WHERE b.condition_concept_id = 0
        AND NOT (
          b.source_concept_id <> 0
          AND b.source_invalid_reason IS NULL
          AND b.source_standard_concept = 'S'
        )
        AND NOT EXISTS (
          SELECT 1
          FROM mapped_targets mt
          WHERE mt.source_domain = b.source_domain
            AND mt.source_record_id = b.source_record_id
        )
    )
    """


def materialize_condition_event_routes(config_path: str) -> int:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_event_routes.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            cte = _routing_cte(source_schema, target_schema)

            expected_rows = int(
                connection.execute(text(cte + " SELECT COUNT_BIG(*) FROM resolved")).scalar_one()
            )
            source_events = int(
                connection.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[condition_occurrence] co
                        JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                          ON x.condition_occurrence_id = co.condition_occurrence_id
                        """
                    )
                ).scalar_one()
            )

            existing = 0
            if table_exists(connection, source_schema, ROUTE_TABLE):
                existing = int(
                    connection.execute(
                        text(f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{ROUTE_TABLE}]")
                    ).scalar_one()
                )

            if existing:
                if existing != expected_rows:
                    raise RuntimeError(
                        f"[{source_schema}].[{ROUTE_TABLE}] already has {existing:,} rows; "
                        f"current routing logic expects {expected_rows:,}. Drop the route table before rerunning "
                        "after a routing-logic change."
                    )
                status = "already_materialized_matched"
            else:
                if table_exists(connection, source_schema, ROUTE_TABLE):
                    connection.exec_driver_sql(f"DROP TABLE [{source_schema}].[{ROUTE_TABLE}]")
                    connection.commit()

                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE [{source_schema}].[{ROUTE_TABLE}] (
                      route_id bigint NOT NULL,
                      source_domain varchar(16) NOT NULL,
                      source_record_id nvarchar(255) NOT NULL,
                      source_condition_occurrence_id bigint NOT NULL,
                      source_concept_id bigint NOT NULL,
                      target_domain varchar(50) NOT NULL,
                      target_concept_id bigint NOT NULL,
                      route_status varchar(64) NOT NULL,
                      source_code_type nvarchar(50) NULL,
                      source_provenance nvarchar(50) NULL,
                      date_basis varchar(32) NOT NULL,
                      CONSTRAINT PK_{ROUTE_TABLE} PRIMARY KEY (route_id),
                      CONSTRAINT UQ_{ROUTE_TABLE}_route UNIQUE
                        (source_domain, source_record_id, target_domain, target_concept_id)
                    )
                    """
                )
                connection.commit()

                insert_sql = cte + f"""
                , numbered AS (
                  SELECT
                    ROW_NUMBER() OVER (
                      ORDER BY source_domain, source_record_id, target_domain, target_concept_id, route_status
                    ) AS route_id,
                    *
                  FROM resolved
                )
                INSERT INTO [{source_schema}].[{ROUTE_TABLE}] (
                  route_id, source_domain, source_record_id,
                  source_condition_occurrence_id, source_concept_id,
                  target_domain, target_concept_id, route_status,
                  source_code_type, source_provenance, date_basis
                )
                SELECT
                  route_id, source_domain, source_record_id,
                  source_condition_occurrence_id, source_concept_id,
                  target_domain, target_concept_id, route_status,
                  source_code_type, source_provenance, date_basis
                FROM numbered
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "materialized"

            actual_rows = int(
                connection.execute(
                    text(f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{ROUTE_TABLE}]")
                ).scalar_one()
            )
            if actual_rows != expected_rows:
                raise RuntimeError(
                    f"Route reconciliation failed: expected={expected_rows:,}, actual={actual_rows:,}"
                )

            summary = connection.execute(
                text(
                    f"""
                    SELECT source_domain, target_domain, route_status,
                           COUNT_BIG(*) AS target_rows,
                           COUNT_BIG(DISTINCT source_record_id) AS source_events
                    FROM [{source_schema}].[{ROUTE_TABLE}]
                    GROUP BY source_domain, target_domain, route_status
                    ORDER BY source_domain, target_rows DESC, target_domain, route_status
                    """
                )
            ).fetchall()
            multiplicity = connection.execute(
                text(
                    f"""
                    SELECT route_count, COUNT_BIG(*) AS source_events
                    FROM (
                      SELECT source_domain, source_record_id, COUNT_BIG(*) AS route_count
                      FROM [{source_schema}].[{ROUTE_TABLE}]
                      GROUP BY source_domain, source_record_id
                    ) x
                    GROUP BY route_count
                    ORDER BY route_count
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_event_routes",
        "status": status,
        "source_events": source_events,
        "route_rows": actual_rows,
        "additional_rows_from_one_to_many_mapping": actual_rows - source_events,
        "routing_rules": {
            "existing_nonzero_condition_mapping": "Condition",
            "valid_standard_source_concept": "use its current vocabulary domain and concept",
            "otherwise": "emit every active valid standard Maps to target",
            "no_valid_target": "emit one unresolved route with target_concept_id 0",
            "core_omop_tables": "not modified by this stage",
        },
        "summary": [
            {
                "source_domain": row[0],
                "target_domain": row[1],
                "route_status": row[2],
                "target_rows": int(row[3]),
                "source_events": int(row[4]),
            }
            for row in summary
        ],
        "source_event_route_multiplicity": [
            {"route_count": int(row[0]), "source_events": int(row[1])}
            for row in multiplicity
        ],
        "interpretation_note": (
            "This is the canonical source-event routing ledger for DIAGNOSIS/CONDITION-derived records. "
            "It preserves one-to-many vocabulary mappings and does not yet write cross-domain records into OMOP core tables."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Condition-derived source events: {source_events:,}")
    print(f"Materialized route rows: {actual_rows:,} [{status}]")
    print(f"Additional one-to-many rows: {actual_rows - source_events:,}")
    print("Routes by source and target domain:")
    for row in summary:
        print(
            f"  {row[0]:10s} -> {row[1]:20s} "
            f"source_events={int(row[4]):,} target_rows={int(row[3]):,} "
            f"status={row[2]}"
        )
    print(f"Audit: {audit_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize the audited DIAGNOSIS/CONDITION source-event routing ledger."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    return materialize_condition_event_routes(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
