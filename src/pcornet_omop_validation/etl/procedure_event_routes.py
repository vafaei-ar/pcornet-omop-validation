from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_procedure_event_route"
EVENT_DOMAINS = (
    "Condition",
    "Device",
    "Drug",
    "Measurement",
    "Observation",
    "Procedure",
    "Specimen",
)


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    for schema, table in (
        (source_schema, "PCORnet_PROCEDURES"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
    ):
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _routing_cte(source_schema: str, target_schema: str) -> str:
    event_domains = ",".join(f"'{domain}'" for domain in EVENT_DOMAINS)
    return f"""
    WITH eligible AS (
      SELECT
        LTRIM(RTRIM(CONVERT(nvarchar(255), PROCEDURESID))) AS source_procedure_id,
        LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) AS patid,
        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), ENCOUNTERID))), '') AS encounterid,
        LTRIM(RTRIM(CONVERT(nvarchar(255), PX))) AS px,
        UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), PX_TYPE)))) AS px_type,
        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), RAW_PX))), '') AS raw_px,
        UPPER(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), RAW_PX_TYPE))), '')) AS raw_px_type,
        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), PX_SOURCE))), '') AS px_source,
        CAST(PX_DATE AS date) AS px_date
      FROM [{source_schema}].[PCORnet_PROCEDURES]
      WHERE PX_DATE IS NOT NULL
        AND PROCEDURESID IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), PROCEDURESID))) <> ''
        AND PATID IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
        AND PX IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), PX))) <> ''
    ),
    code_keys AS (
      SELECT DISTINCT
        px_type,
        px,
        CASE WHEN px_type = 'OT' THEN COALESCE(raw_px_type, '') ELSE '' END AS raw_px_type_key
      FROM eligible
    ),
    source_candidates AS (
      SELECT
        k.px_type,
        k.px,
        k.raw_px_type_key,
        c.concept_id AS source_concept_id,
        c.vocabulary_id AS source_vocabulary_id,
        c.domain_id AS source_concept_domain,
        c.standard_concept AS source_standard_concept,
        c.invalid_reason AS source_invalid_reason
      FROM code_keys k
      LEFT JOIN [{target_schema}].[concept] c
        ON c.concept_code = k.px
       AND (
            (k.px_type = 'CH' AND c.vocabulary_id IN ('CPT4','HCPCS'))
         OR (k.px_type = '09' AND c.vocabulary_id = 'ICD9Proc')
         OR (k.px_type = '10' AND c.vocabulary_id = 'ICD10PCS')
         OR (k.px_type = 'OT' AND k.raw_px_type_key = 'SNOMED CT' AND c.vocabulary_id = 'SNOMED')
       )
    ),
    candidate_counts AS (
      SELECT
        px_type,
        px,
        raw_px_type_key,
        COUNT(DISTINCT source_concept_id) AS source_concept_count
      FROM source_candidates
      GROUP BY px_type, px, raw_px_type_key
    ),
    unique_source AS (
      SELECT sc.*
      FROM source_candidates sc
      JOIN candidate_counts cc
        ON cc.px_type = sc.px_type
       AND cc.px = sc.px
       AND cc.raw_px_type_key = sc.raw_px_type_key
      WHERE cc.source_concept_count = 1
        AND sc.source_concept_id IS NOT NULL
    ),
    direct_standard AS (
      SELECT
        us.px_type,
        us.px,
        us.raw_px_type_key,
        us.source_concept_id,
        us.source_vocabulary_id,
        us.source_concept_domain,
        us.source_concept_domain AS target_domain,
        us.source_concept_id AS target_concept_id,
        CAST('direct_standard_source_concept' AS varchar(64)) AS route_status
      FROM unique_source us
      WHERE us.source_invalid_reason IS NULL
        AND us.source_standard_concept = 'S'
    ),
    mapped_standard AS (
      SELECT DISTINCT
        us.px_type,
        us.px,
        us.raw_px_type_key,
        us.source_concept_id,
        us.source_vocabulary_id,
        us.source_concept_domain,
        tgt.domain_id AS target_domain,
        tgt.concept_id AS target_concept_id,
        CAST('maps_to_standard' AS varchar(64)) AS route_status
      FROM unique_source us
      JOIN [{target_schema}].[concept_relationship] cr
        ON cr.concept_id_1 = us.source_concept_id
       AND cr.relationship_id = 'Maps to'
       AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
      JOIN [{target_schema}].[concept] tgt
        ON tgt.concept_id = cr.concept_id_2
       AND tgt.standard_concept = 'S'
       AND tgt.invalid_reason IS NULL
      WHERE NOT (
        us.source_invalid_reason IS NULL
        AND us.source_standard_concept = 'S'
      )
    ),
    resolved_standard AS (
      SELECT * FROM direct_standard
      UNION ALL
      SELECT * FROM mapped_standard
    ),
    fallback AS (
      SELECT
        cc.px_type,
        cc.px,
        cc.raw_px_type_key,
        COALESCE(us.source_concept_id, 0) AS source_concept_id,
        us.source_vocabulary_id,
        us.source_concept_domain,
        CAST(
          CASE
            WHEN cc.source_concept_count = 1 AND us.source_concept_domain IS NOT NULL
              THEN us.source_concept_domain
            ELSE 'Procedure'
          END
          AS varchar(50)
        ) AS target_domain,
        CAST(0 AS bigint) AS target_concept_id,
        CAST(
          CASE
            WHEN cc.px_type = 'OT' AND cc.raw_px_type_key <> 'SNOMED CT'
              THEN 'unsupported_raw_px_type_fallback_procedure'
            WHEN cc.source_concept_count = 0
              THEN 'source_concept_not_found_fallback_procedure'
            WHEN cc.source_concept_count > 1
              THEN 'ambiguous_source_concept_fallback_procedure'
            ELSE 'no_active_standard_target_source_domain'
          END
          AS varchar(64)
        ) AS route_status
      FROM candidate_counts cc
      LEFT JOIN unique_source us
        ON us.px_type = cc.px_type
       AND us.px = cc.px
       AND us.raw_px_type_key = cc.raw_px_type_key
      WHERE NOT EXISTS (
        SELECT 1
        FROM resolved_standard rs
        WHERE rs.px_type = cc.px_type
          AND rs.px = cc.px
          AND rs.raw_px_type_key = cc.raw_px_type_key
      )
    ),
    code_routes AS (
      SELECT * FROM resolved_standard
      UNION ALL
      SELECT * FROM fallback
    ),
    event_routes AS (
      SELECT
        e.source_procedure_id,
        e.patid,
        e.encounterid,
        e.px,
        e.px_type,
        e.raw_px,
        e.raw_px_type,
        e.px_source,
        e.px_date,
        cr.source_concept_id,
        cr.source_vocabulary_id,
        cr.source_concept_domain,
        cr.target_domain,
        cr.target_concept_id,
        cr.route_status,
        CAST(
          CASE
            WHEN cr.target_concept_id = 0 THEN 'unresolved'
            WHEN cr.target_domain IN ({event_domains}) THEN 'event_route'
            ELSE 'non_event_semantic_component'
          END
          AS varchar(40)
        ) AS disposition
      FROM eligible e
      JOIN code_routes cr
        ON cr.px_type = e.px_type
       AND cr.px = e.px
       AND cr.raw_px_type_key = CASE WHEN e.px_type = 'OT' THEN COALESCE(e.raw_px_type, '') ELSE '' END
    ),
    numbered AS (
      SELECT
        ROW_NUMBER() OVER (
          ORDER BY source_procedure_id, target_domain, target_concept_id, route_status
        ) AS route_id,
        ROW_NUMBER() OVER (
          PARTITION BY source_procedure_id
          ORDER BY target_domain, target_concept_id, route_status
        ) AS target_ordinal,
        *
      FROM event_routes
    )
    """


def materialize_procedure_event_routes(config_path: str, replace: bool = False) -> int:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_event_routes.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            exists = table_exists(connection, source_schema, ROUTE_TABLE)
            if exists and not replace:
                raise RuntimeError(
                    f"[{source_schema}].[{ROUTE_TABLE}] already exists. "
                    "Use --replace to rebuild it after vocabulary or routing-rule changes."
                )
            if exists:
                connection.exec_driver_sql(f"DROP TABLE [{source_schema}].[{ROUTE_TABLE}]")
                connection.commit()

            cte = _routing_cte(source_schema, target_schema)
            source_events = int(
                connection.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[PCORnet_PROCEDURES]
                        WHERE PX_DATE IS NOT NULL
                          AND PROCEDURESID IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), PROCEDURESID))) <> ''
                          AND PATID IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
                          AND PX IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), PX))) <> ''
                        """
                    )
                ).scalar_one()
            )
            expected_rows = int(
                connection.execute(text(cte + " SELECT COUNT_BIG(*) FROM numbered")).scalar_one()
            )

            connection.exec_driver_sql(
                f"""
                CREATE TABLE [{source_schema}].[{ROUTE_TABLE}] (
                  route_id bigint NOT NULL,
                  source_procedure_id nvarchar(255) NOT NULL,
                  target_ordinal int NOT NULL,
                  patid nvarchar(255) NOT NULL,
                  encounterid nvarchar(255) NULL,
                  px nvarchar(255) NOT NULL,
                  px_type nvarchar(20) NOT NULL,
                  raw_px nvarchar(255) NULL,
                  raw_px_type nvarchar(100) NULL,
                  px_source nvarchar(50) NULL,
                  px_date date NOT NULL,
                  source_concept_id bigint NOT NULL,
                  source_vocabulary_id varchar(20) NULL,
                  source_concept_domain varchar(50) NULL,
                  target_domain varchar(50) NOT NULL,
                  target_concept_id bigint NOT NULL,
                  route_status varchar(64) NOT NULL,
                  disposition varchar(40) NOT NULL,
                  CONSTRAINT PK_{ROUTE_TABLE} PRIMARY KEY (route_id),
                  CONSTRAINT UQ_{ROUTE_TABLE}_ordinal UNIQUE (source_procedure_id, target_ordinal),
                  CONSTRAINT UQ_{ROUTE_TABLE}_target UNIQUE
                    (source_procedure_id, target_domain, target_concept_id)
                )
                """
            )
            connection.commit()

            insert_sql = cte + f"""
            INSERT INTO [{source_schema}].[{ROUTE_TABLE}] (
              route_id, source_procedure_id, target_ordinal,
              patid, encounterid, px, px_type, raw_px, raw_px_type, px_source, px_date,
              source_concept_id, source_vocabulary_id, source_concept_domain,
              target_domain, target_concept_id, route_status, disposition
            )
            SELECT
              route_id, source_procedure_id, target_ordinal,
              patid, encounterid, px, px_type, raw_px, raw_px_type, px_source, px_date,
              source_concept_id, source_vocabulary_id, source_concept_domain,
              target_domain, target_concept_id, route_status, disposition
            FROM numbered
            """
            connection.exec_driver_sql(insert_sql)
            connection.commit()

            actual_rows = int(
                connection.execute(
                    text(f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{ROUTE_TABLE}]")
                ).scalar_one()
            )
            routed_events = int(
                connection.execute(
                    text(
                        f"SELECT COUNT_BIG(DISTINCT source_procedure_id) "
                        f"FROM [{source_schema}].[{ROUTE_TABLE}]"
                    )
                ).scalar_one()
            )
            if actual_rows != expected_rows or routed_events != source_events:
                raise RuntimeError(
                    "Procedure route reconciliation failed: "
                    f"source_events={source_events:,}, routed_events={routed_events:,}, "
                    f"expected_rows={expected_rows:,}, actual_rows={actual_rows:,}"
                )

            summary = connection.execute(
                text(
                    f"""
                    SELECT target_domain, disposition, route_status,
                           COUNT_BIG(*) AS target_rows,
                           COUNT_BIG(DISTINCT source_procedure_id) AS source_events
                    FROM [{source_schema}].[{ROUTE_TABLE}]
                    GROUP BY target_domain, disposition, route_status
                    ORDER BY target_rows DESC, target_domain, route_status
                    """
                )
            ).fetchall()
            multiplicity = connection.execute(
                text(
                    f"""
                    SELECT route_count, COUNT_BIG(*) AS source_events
                    FROM (
                      SELECT source_procedure_id, COUNT_BIG(*) AS route_count
                      FROM [{source_schema}].[{ROUTE_TABLE}]
                      GROUP BY source_procedure_id
                    ) x
                    GROUP BY route_count
                    ORDER BY route_count
                    """
                )
            ).fetchall()
            disposition_rows = connection.execute(
                text(
                    f"""
                    SELECT disposition, COUNT_BIG(*) AS route_rows,
                           COUNT_BIG(DISTINCT source_procedure_id) AS source_events
                    FROM [{source_schema}].[{ROUTE_TABLE}]
                    GROUP BY disposition
                    ORDER BY route_rows DESC, disposition
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "procedure_event_routes",
        "source_events": source_events,
        "route_rows": actual_rows,
        "additional_rows_from_one_to_many_mapping": actual_rows - source_events,
        "routing_rules": {
            "09": "ICD9Proc lookup by PX",
            "10": "ICD10PCS lookup by PX",
            "CH": "CPT4/HCPCS lookup by PX",
            "OT": "SNOMED lookup by PX when RAW_PX_TYPE='SNOMED CT'",
            "direct_standard": "route to the source Standard Concept and its domain",
            "mapped_standard": "preserve every active Standard 'Maps to' target; no TOP(1) selection",
            "no_standard_target": "preserve source concept/domain when known with target_concept_id=0",
            "unknown_or_ambiguous_source": "fallback to Procedure domain with target_concept_id=0",
            "non_event_domains": "retain in the ledger as semantic components; do not create standalone OMOP events",
            "core_omop_tables": "not modified by this stage",
        },
        "dispositions": [
            {
                "disposition": row[0],
                "route_rows": int(row[1]),
                "source_events": int(row[2]),
            }
            for row in disposition_rows
        ],
        "summary": [
            {
                "target_domain": row[0],
                "disposition": row[1],
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
            "Canonical native PCORnet PROCEDURES routing ledger. It preserves cross-domain and one-to-many "
            "vocabulary semantics while separating event routes, non-event semantic components, and unresolved "
            "records. No OMOP core fact table is written by this stage."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Native PROCEDURES source events: {source_events:,}")
    print(f"Materialized route rows: {actual_rows:,}")
    print(f"Additional one-to-many rows: {actual_rows - source_events:,}")
    print("Disposition summary:")
    for row in disposition_rows:
        print(
            f"  {row[0]:30s} route_rows={int(row[1]):,} "
            f"source_events={int(row[2]):,}"
        )
    print("Routes by target domain/status:")
    for row in summary:
        print(
            f"  {str(row[0]):20s} {row[1]:30s} {row[2]:45s} "
            f"source_events={int(row[4]):,} target_rows={int(row[3]):,}"
        )
    print(f"Audit: {audit_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize the audited native PCORnet PROCEDURES source-event routing ledger."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Drop and rebuild an existing route ledger after vocabulary or routing-rule changes.",
    )
    args = parser.parse_args(argv)
    return materialize_procedure_event_routes(args.config, replace=args.replace)


if __name__ == "__main__":
    raise SystemExit(main())
