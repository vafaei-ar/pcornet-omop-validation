from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit native PCORnet PROCEDURES vocabulary mapping and OMOP target-domain semantics."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_mapping_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            required = (
                (source_schema, "PCORnet_PROCEDURES"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            )
            for schema, table in required:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            sql = f"""
            WITH eligible AS (
              SELECT
                PROCEDURESID,
                LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
                UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) AS px_type
              FROM [{source_schema}].[PCORnet_PROCEDURES]
              WHERE PX_DATE IS NOT NULL
                AND PROCEDURESID IS NOT NULL
                AND LTRIM(RTRIM(CAST(PROCEDURESID AS nvarchar(255)))) <> ''
                AND PATID IS NOT NULL
                AND LTRIM(RTRIM(CAST(PATID AS nvarchar(255)))) <> ''
                AND PX IS NOT NULL
                AND LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) <> ''
            ), code_counts AS (
              SELECT px_type, px, COUNT_BIG(*) AS source_rows
              FROM eligible
              GROUP BY px_type, px
            ), source_candidates AS (
              SELECT
                cc.px_type,
                cc.px,
                cc.source_rows,
                c.concept_id AS source_concept_id,
                c.vocabulary_id,
                c.domain_id AS source_domain_id,
                c.standard_concept AS source_standard_concept,
                c.invalid_reason AS source_invalid_reason
              FROM code_counts cc
              LEFT JOIN [{target_schema}].[concept] c
                ON c.concept_code = cc.px
               AND (
                    (cc.px_type = 'CH' AND c.vocabulary_id IN ('CPT4','HCPCS'))
                 OR (cc.px_type = '09' AND c.vocabulary_id = 'ICD9Proc')
                 OR (cc.px_type = '10' AND c.vocabulary_id = 'ICD10PCS')
               )
            ), candidate_counts AS (
              SELECT
                px_type,
                px,
                MAX(source_rows) AS source_rows,
                COUNT(DISTINCT source_concept_id) AS source_concept_count
              FROM source_candidates
              GROUP BY px_type, px
            ), unique_source AS (
              SELECT sc.*
              FROM source_candidates sc
              JOIN candidate_counts cc
                ON cc.px_type = sc.px_type
               AND cc.px = sc.px
              WHERE cc.source_concept_count = 1
                AND sc.source_concept_id IS NOT NULL
            ), mapped_targets AS (
              SELECT DISTINCT
                us.px_type,
                us.px,
                us.source_rows,
                us.source_concept_id,
                us.vocabulary_id,
                us.source_domain_id,
                us.source_standard_concept,
                us.source_invalid_reason,
                tgt.concept_id AS target_concept_id,
                tgt.domain_id AS target_domain_id
              FROM unique_source us
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = us.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.invalid_reason IS NULL
            ), target_summary AS (
              SELECT
                us.px_type,
                us.px,
                COUNT(DISTINCT mt.target_concept_id) AS target_concept_count,
                COUNT(DISTINCT mt.target_domain_id) AS target_domain_count,
                MAX(CASE WHEN mt.target_domain_id = 'Procedure' THEN 1 ELSE 0 END) AS has_procedure_target,
                MAX(CASE WHEN mt.target_domain_id <> 'Procedure' THEN 1 ELSE 0 END) AS has_other_domain_target
              FROM unique_source us
              LEFT JOIN mapped_targets mt
                ON mt.px_type = us.px_type
               AND mt.px = us.px
              GROUP BY us.px_type, us.px
            ), classified AS (
              SELECT
                cc.px_type,
                cc.px,
                cc.source_rows,
                cc.source_concept_count,
                us.source_concept_id,
                us.vocabulary_id,
                us.source_domain_id,
                us.source_standard_concept,
                us.source_invalid_reason,
                COALESCE(ts.target_concept_count, 0) AS target_concept_count,
                COALESCE(ts.target_domain_count, 0) AS target_domain_count,
                COALESCE(ts.has_procedure_target, 0) AS has_procedure_target,
                COALESCE(ts.has_other_domain_target, 0) AS has_other_domain_target,
                CASE
                  WHEN cc.px_type = 'OT' THEN 'unsupported_px_type'
                  WHEN cc.source_concept_count = 0 THEN 'source_concept_not_found'
                  WHEN cc.source_concept_count > 1 THEN 'ambiguous_source_concept'
                  WHEN us.source_invalid_reason IS NULL
                       AND us.source_standard_concept = 'S'
                       AND us.source_domain_id = 'Procedure'
                    THEN 'direct_standard_procedure'
                  WHEN us.source_invalid_reason IS NULL
                       AND us.source_standard_concept = 'S'
                       AND us.source_domain_id <> 'Procedure'
                    THEN 'direct_standard_other_domain'
                  WHEN COALESCE(ts.target_concept_count, 0) = 0 THEN 'no_active_standard_target'
                  WHEN COALESCE(ts.target_domain_count, 0) > 1 THEN 'maps_to_multiple_domains'
                  WHEN COALESCE(ts.has_procedure_target, 0) = 1 THEN 'maps_to_procedure'
                  ELSE 'maps_to_other_domain'
                END AS mapping_outcome
              FROM candidate_counts cc
              LEFT JOIN unique_source us
                ON us.px_type = cc.px_type
               AND us.px = cc.px
              LEFT JOIN target_summary ts
                ON ts.px_type = cc.px_type
               AND ts.px = cc.px
            )
            SELECT
              px_type,
              mapping_outcome,
              SUM(source_rows) AS source_rows,
              COUNT_BIG(*) AS distinct_codes
            FROM classified
            GROUP BY px_type, mapping_outcome
            ORDER BY px_type, source_rows DESC, mapping_outcome
            """
            summary_rows = connection.execute(text(sql)).fetchall()

            top_sql = sql.replace(
                "SELECT\n              px_type,\n              mapping_outcome,\n              SUM(source_rows) AS source_rows,\n              COUNT_BIG(*) AS distinct_codes\n            FROM classified\n            GROUP BY px_type, mapping_outcome\n            ORDER BY px_type, source_rows DESC, mapping_outcome",
                "SELECT TOP (100) px_type, px, source_rows, source_concept_count, source_concept_id, vocabulary_id, source_domain_id, source_standard_concept, source_invalid_reason, target_concept_count, target_domain_count, mapping_outcome FROM classified WHERE mapping_outcome NOT IN ('direct_standard_procedure','maps_to_procedure') ORDER BY source_rows DESC, px_type, px"
            )
            top_rows = connection.execute(text(top_sql)).mappings().all()

            target_sql = f"""
            WITH eligible AS (
              SELECT LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
                     UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) AS px_type
              FROM [{source_schema}].[PCORnet_PROCEDURES]
              WHERE PX_DATE IS NOT NULL
                AND PX IS NOT NULL
                AND LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) <> ''
            ), code_counts AS (
              SELECT px_type, px, COUNT_BIG(*) AS source_rows FROM eligible GROUP BY px_type, px
            ), source_candidates AS (
              SELECT cc.px_type, cc.px, cc.source_rows, c.concept_id AS source_concept_id
              FROM code_counts cc
              JOIN [{target_schema}].[concept] c
                ON c.concept_code = cc.px
               AND ((cc.px_type='CH' AND c.vocabulary_id IN ('CPT4','HCPCS'))
                 OR (cc.px_type='09' AND c.vocabulary_id='ICD9Proc')
                 OR (cc.px_type='10' AND c.vocabulary_id='ICD10PCS'))
            ), unique_source AS (
              SELECT sc.* FROM source_candidates sc
              JOIN (
                SELECT px_type, px, COUNT(DISTINCT source_concept_id) AS n
                FROM source_candidates GROUP BY px_type, px
              ) x ON x.px_type=sc.px_type AND x.px=sc.px AND x.n=1
            ), targets AS (
              SELECT DISTINCT us.px_type, us.px, us.source_rows, tgt.concept_id, tgt.domain_id
              FROM unique_source us
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1=us.source_concept_id
               AND cr.relationship_id='Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason='')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id=cr.concept_id_2
               AND tgt.standard_concept='S'
               AND tgt.invalid_reason IS NULL
            )
            SELECT domain_id, SUM(source_rows) AS source_event_rows,
                   SUM(source_rows) AS minimum_target_rows,
                   COUNT_BIG(*) AS distinct_code_target_pairs
            FROM targets
            GROUP BY domain_id
            ORDER BY source_event_rows DESC, domain_id
            """
            target_rows = connection.execute(text(target_sql)).fetchall()

            vocab_rows = connection.execute(
                text(
                    f"""
                    SELECT vocabulary_id,
                           COUNT_BIG(*) AS concepts,
                           SUM(CASE WHEN invalid_reason IS NULL THEN 1 ELSE 0 END) AS active_concepts
                    FROM [{target_schema}].[concept]
                    WHERE vocabulary_id IN ('CPT4','HCPCS','ICD9Proc','ICD10PCS')
                    GROUP BY vocabulary_id
                    ORDER BY vocabulary_id
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "procedure_mapping_audit",
        "mapping_rule": {
            "CH": ["CPT4", "HCPCS"],
            "09": ["ICD9Proc"],
            "10": ["ICD10PCS"],
            "OT": [],
            "note": "PX_TYPE is used to constrain source-vocabulary lookup; no broad vocabulary guessing is performed.",
        },
        "summary": [
            {
                "px_type": row[0],
                "mapping_outcome": row[1],
                "source_rows": int(row[2] or 0),
                "distinct_codes": int(row[3] or 0),
            }
            for row in summary_rows
        ],
        "target_domains_from_maps_to": [
            {
                "target_domain": row[0],
                "source_event_rows": int(row[1] or 0),
                "minimum_target_rows": int(row[2] or 0),
                "distinct_code_target_pairs": int(row[3] or 0),
            }
            for row in target_rows
        ],
        "vocabulary_presence": [
            {
                "vocabulary_id": row[0],
                "concepts": int(row[1] or 0),
                "active_concepts": int(row[2] or 0),
            }
            for row in vocab_rows
        ],
        "top_100_nonprocedure_or_unresolved_codes": [dict(row) for row in top_rows],
        "interpretation_note": (
            "This audit is read-only. It checks native PCORnet PROCEDURES mapping semantics before writing procedure_occurrence. "
            "OMOP Procedure records should use Standard Concepts in the Procedure domain; cross-domain mappings must be routed deliberately rather than silently forced into procedure_occurrence."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    print("Native PROCEDURES mapping outcomes:")
    for row in summary_rows:
        print(f"  PX_TYPE={row[0]:>2s}  {row[1]:32s} rows={int(row[2]):,} codes={int(row[3]):,}")
    print("Vocabulary presence:")
    for row in vocab_rows:
        print(f"  {row[0]:10s} concepts={int(row[1]):,} active={int(row[2] or 0):,}")
    print("Mapped target-domain distribution:")
    for row in target_rows:
        print(f"  {row[0]:20s} source_event_rows={int(row[1]):,}")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
