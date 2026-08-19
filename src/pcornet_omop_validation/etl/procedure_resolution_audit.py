from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve remaining native PCORnet PROCEDURES vocabulary ambiguities before routing."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_resolution_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            for schema, table in (
                (source_schema, "PCORnet_PROCEDURES"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            ):
                if not table_exists(connection, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            ot_sql = f"""
            WITH eligible AS (
              SELECT
                LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
                LTRIM(RTRIM(CAST(RAW_PX AS nvarchar(255)))) AS raw_px
              FROM [{source_schema}].[PCORnet_PROCEDURES]
              WHERE PX_DATE IS NOT NULL
                AND UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) = 'OT'
                AND UPPER(LTRIM(RTRIM(CAST(RAW_PX_TYPE AS nvarchar(100))))) = 'SNOMED CT'
                AND PX IS NOT NULL
                AND LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) <> ''
            ), counts AS (
              SELECT px, raw_px, COUNT_BIG(*) AS source_rows
              FROM eligible
              GROUP BY px, raw_px
            ), candidate_codes AS (
              SELECT cts.*, c.concept_id AS source_concept_id,
                     c.domain_id AS source_domain_id,
                     c.standard_concept AS source_standard_concept,
                     c.invalid_reason AS source_invalid_reason
              FROM counts cts
              LEFT JOIN [{target_schema}].[concept] c
                ON c.vocabulary_id = 'SNOMED'
               AND c.concept_code = cts.px
            ), mapped AS (
              SELECT DISTINCT
                cc.px, cc.raw_px, cc.source_rows,
                cc.source_concept_id, cc.source_domain_id,
                cc.source_standard_concept, cc.source_invalid_reason,
                tgt.concept_id AS target_concept_id,
                tgt.domain_id AS target_domain_id
              FROM candidate_codes cc
              LEFT JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = cc.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              LEFT JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.invalid_reason IS NULL
            ), classified AS (
              SELECT
                px, raw_px, MAX(source_rows) AS source_rows,
                COUNT(DISTINCT source_concept_id) AS source_concept_count,
                MAX(CASE WHEN source_invalid_reason IS NULL AND source_standard_concept='S'
                         THEN 1 ELSE 0 END) AS has_direct_standard,
                COUNT(DISTINCT target_concept_id) AS target_concept_count,
                COUNT(DISTINCT target_domain_id) AS target_domain_count,
                MAX(source_domain_id) AS direct_domain_id
              FROM mapped
              GROUP BY px, raw_px
            )
            SELECT
              CASE
                WHEN source_concept_count = 0 THEN 'source_concept_not_found'
                WHEN source_concept_count > 1 THEN 'ambiguous_source_concept'
                WHEN has_direct_standard = 1 THEN 'direct_standard'
                WHEN target_concept_count = 0 THEN 'no_active_standard_target'
                WHEN target_domain_count > 1 THEN 'maps_to_multiple_domains'
                ELSE 'maps_to_standard'
              END AS mapping_outcome,
              SUM(source_rows) AS source_rows,
              COUNT_BIG(*) AS distinct_px_raw_pairs
            FROM classified
            GROUP BY CASE
                WHEN source_concept_count = 0 THEN 'source_concept_not_found'
                WHEN source_concept_count > 1 THEN 'ambiguous_source_concept'
                WHEN has_direct_standard = 1 THEN 'direct_standard'
                WHEN target_concept_count = 0 THEN 'no_active_standard_target'
                WHEN target_domain_count > 1 THEN 'maps_to_multiple_domains'
                ELSE 'maps_to_standard'
              END
            ORDER BY source_rows DESC, mapping_outcome
            """
            ot_summary = connection.execute(text(ot_sql)).fetchall()

            ot_domain_sql = f"""
            WITH eligible AS (
              SELECT LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
                     COUNT_BIG(*) AS source_rows
              FROM [{source_schema}].[PCORnet_PROCEDURES]
              WHERE PX_DATE IS NOT NULL
                AND UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) = 'OT'
                AND UPPER(LTRIM(RTRIM(CAST(RAW_PX_TYPE AS nvarchar(100))))) = 'SNOMED CT'
                AND PX IS NOT NULL
                AND LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) <> ''
              GROUP BY LTRIM(RTRIM(CAST(PX AS nvarchar(255))))
            ), src AS (
              SELECT e.*, c.concept_id, c.domain_id, c.standard_concept, c.invalid_reason
              FROM eligible e
              JOIN [{target_schema}].[concept] c
                ON c.vocabulary_id='SNOMED' AND c.concept_code=e.px
            ), direct_standard AS (
              SELECT px, source_rows, domain_id AS target_domain_id, concept_id AS target_concept_id
              FROM src
              WHERE invalid_reason IS NULL AND standard_concept='S'
            ), mapped_standard AS (
              SELECT DISTINCT s.px, s.source_rows, tgt.domain_id AS target_domain_id,
                              tgt.concept_id AS target_concept_id
              FROM src s
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1=s.concept_id
               AND cr.relationship_id='Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason='')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id=cr.concept_id_2
               AND tgt.standard_concept='S'
               AND tgt.invalid_reason IS NULL
              WHERE NOT (s.invalid_reason IS NULL AND s.standard_concept='S')
            ), targets AS (
              SELECT * FROM direct_standard
              UNION ALL
              SELECT * FROM mapped_standard
            )
            SELECT target_domain_id, SUM(source_rows) AS source_event_rows,
                   COUNT(DISTINCT px) AS distinct_px_codes,
                   COUNT_BIG(*) AS target_pairs
            FROM targets
            GROUP BY target_domain_id
            ORDER BY source_event_rows DESC, target_domain_id
            """
            ot_domains = connection.execute(text(ot_domain_sql)).fetchall()

            problem_sql = f"""
            WITH eligible AS (
              SELECT
                UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) AS px_type,
                LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
                COUNT_BIG(*) AS source_rows
              FROM [{source_schema}].[PCORnet_PROCEDURES]
              WHERE PX_DATE IS NOT NULL
                AND PX IS NOT NULL
                AND LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) <> ''
                AND UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) IN ('CH','09','10')
              GROUP BY UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))),
                       LTRIM(RTRIM(CAST(PX AS nvarchar(255))))
            ), candidates AS (
              SELECT e.*, c.concept_id AS source_concept_id, c.vocabulary_id,
                     c.domain_id AS source_domain_id, c.standard_concept,
                     c.invalid_reason AS source_invalid_reason
              FROM eligible e
              LEFT JOIN [{target_schema}].[concept] c
                ON c.concept_code=e.px
               AND ((e.px_type='CH' AND c.vocabulary_id IN ('CPT4','HCPCS'))
                 OR (e.px_type='09' AND c.vocabulary_id='ICD9Proc')
                 OR (e.px_type='10' AND c.vocabulary_id='ICD10PCS'))
            ), candidate_counts AS (
              SELECT px_type, px, MAX(source_rows) AS source_rows,
                     COUNT(DISTINCT source_concept_id) AS source_concept_count
              FROM candidates GROUP BY px_type, px
            ), unique_source AS (
              SELECT c.* FROM candidates c
              JOIN candidate_counts x
                ON x.px_type=c.px_type AND x.px=c.px
              WHERE x.source_concept_count=1 AND c.source_concept_id IS NOT NULL
            ), targets AS (
              SELECT DISTINCT u.px_type, u.px, tgt.concept_id AS target_concept_id,
                              tgt.domain_id AS target_domain_id
              FROM unique_source u
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1=u.source_concept_id
               AND cr.relationship_id='Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason='')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id=cr.concept_id_2
               AND tgt.standard_concept='S'
               AND tgt.invalid_reason IS NULL
            ), summary AS (
              SELECT x.px_type, x.px, x.source_rows, x.source_concept_count,
                     COUNT(DISTINCT t.target_concept_id) AS target_concept_count,
                     COUNT(DISTINCT t.target_domain_id) AS target_domain_count
              FROM candidate_counts x
              LEFT JOIN targets t ON t.px_type=x.px_type AND t.px=x.px
              GROUP BY x.px_type, x.px, x.source_rows, x.source_concept_count
            )
            SELECT s.px_type, s.px, s.source_rows,
                   CASE
                     WHEN s.source_concept_count=0 THEN 'source_concept_not_found'
                     WHEN s.source_concept_count>1 THEN 'ambiguous_source_concept'
                     WHEN s.target_concept_count=0 THEN 'no_active_standard_target'
                     WHEN s.target_domain_count>1 THEN 'maps_to_multiple_domains'
                     ELSE 'other'
                   END AS problem_type,
                   c.source_concept_id, c.vocabulary_id, c.source_domain_id,
                   c.standard_concept, c.source_invalid_reason,
                   t.target_concept_id, t.target_domain_id
            FROM summary s
            LEFT JOIN candidates c ON c.px_type=s.px_type AND c.px=s.px
            LEFT JOIN targets t ON t.px_type=s.px_type AND t.px=s.px
            WHERE s.source_concept_count=0
               OR s.source_concept_count>1
               OR s.target_concept_count=0
               OR s.target_domain_count>1
            ORDER BY s.px_type, s.source_rows DESC, s.px,
                     c.vocabulary_id, c.source_concept_id, t.target_domain_id, t.target_concept_id
            """
            problem_rows = connection.execute(text(problem_sql)).mappings().all()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "procedure_resolution_audit",
        "ot_rule_tested": "PX_TYPE=OT and RAW_PX_TYPE='SNOMED CT' -> SNOMED vocabulary lookup by PX",
        "ot_mapping_summary": [
            {
                "mapping_outcome": r[0],
                "source_rows": int(r[1] or 0),
                "distinct_px_raw_pairs": int(r[2] or 0),
            }
            for r in ot_summary
        ],
        "ot_target_domains": [
            {
                "target_domain": r[0],
                "source_event_rows": int(r[1] or 0),
                "distinct_px_codes": int(r[2] or 0),
                "target_pairs": int(r[3] or 0),
            }
            for r in ot_domains
        ],
        "remaining_problem_codes": [dict(r) for r in problem_rows],
        "interpretation_note": (
            "Read-only audit. No OMOP fact rows are written. OT rows are tested against SNOMED because all observed OT rows report RAW_PX_TYPE='SNOMED CT'. Remaining CH/09/10 problem codes are emitted with source candidates and all active standard targets for deterministic policy review."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    print("OT SNOMED mapping outcomes:")
    for r in ot_summary:
        print(f"  {r[0]:30s} rows={int(r[1]):,} px/raw_pairs={int(r[2]):,}")
    print("OT SNOMED target domains:")
    for r in ot_domains:
        print(f"  {str(r[0]):20s} rows={int(r[1]):,} codes={int(r[2]):,} target_pairs={int(r[3]):,}")
    print(f"Remaining CH/09/10 problem detail rows: {len(problem_rows):,}")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
