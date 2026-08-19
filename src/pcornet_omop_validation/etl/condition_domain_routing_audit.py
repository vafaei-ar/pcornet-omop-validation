from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


@dataclass(frozen=True)
class ConditionDomainRoutingAuditResult:
    audited_rows: int
    proposed_condition_rows: int
    cross_domain_rows: int
    unresolved_rows: int
    ambiguous_rows: int
    audit_path: Path


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


def audit_condition_domain_routing(config: EtlConfig) -> ConditionDomainRoutingAuditResult:
    """Quantify destination domains implied by current Athena mappings.

    This is read-only. It does not reroute or delete any OMOP records. Existing
    nonzero condition_concept_id rows are treated as Condition-domain events.
    For zero rows, a current standard source concept is used directly; otherwise
    active Maps to relationships are summarized. Only a unique target domain is
    considered routable. Multi-domain mappings remain ambiguous for review.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_domain_routing_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            base_cte = f"""
            WITH routed AS (
              SELECT
                x.source_domain,
                x.source_code_type,
                co.condition_occurrence_id,
                co.condition_source_value,
                co.condition_source_concept_id,
                co.condition_concept_id,
                src.vocabulary_id AS source_vocabulary_id,
                src.domain_id AS source_concept_domain,
                src.standard_concept AS source_standard_concept,
                src.invalid_reason AS source_invalid_reason,
                rel.standard_target_count,
                rel.standard_target_domain_count,
                rel.single_target_domain,
                CASE
                  WHEN co.condition_concept_id <> 0 THEN 'existing_condition_mapping'
                  WHEN co.condition_source_concept_id = 0 THEN 'unresolved_source_concept'
                  WHEN src.invalid_reason IS NULL AND src.standard_concept = 'S'
                    THEN 'direct_standard_source_concept'
                  WHEN rel.standard_target_domain_count = 1 THEN 'unique_mapped_target_domain'
                  WHEN rel.standard_target_domain_count > 1 THEN 'ambiguous_multiple_target_domains'
                  ELSE 'no_active_standard_target'
                END AS routing_status,
                CASE
                  WHEN co.condition_concept_id <> 0 THEN 'Condition'
                  WHEN co.condition_source_concept_id = 0 THEN NULL
                  WHEN src.invalid_reason IS NULL AND src.standard_concept = 'S'
                    THEN src.domain_id
                  WHEN rel.standard_target_domain_count = 1 THEN rel.single_target_domain
                  ELSE NULL
                END AS proposed_domain
              FROM [{target_schema}].[condition_occurrence] co
              JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                ON x.condition_occurrence_id = co.condition_occurrence_id
              LEFT JOIN [{target_schema}].[concept] src
                ON src.concept_id = co.condition_source_concept_id
              OUTER APPLY (
                SELECT
                  COUNT_BIG(*) AS standard_target_count,
                  COUNT(DISTINCT tgt.domain_id) AS standard_target_domain_count,
                  MIN(tgt.domain_id) AS single_target_domain
                FROM [{target_schema}].[concept_relationship] cr
                JOIN [{target_schema}].[concept] tgt
                  ON tgt.concept_id = cr.concept_id_2
                WHERE cr.concept_id_1 = co.condition_source_concept_id
                  AND cr.relationship_id = 'Maps to'
                  AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                  AND tgt.standard_concept = 'S'
                  AND tgt.invalid_reason IS NULL
              ) rel
            )
            """

            domain_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT source_domain,
                           COALESCE(proposed_domain, '(unresolved)') AS proposed_domain,
                           routing_status,
                           COUNT_BIG(*) AS n
                    FROM routed
                    GROUP BY source_domain, COALESCE(proposed_domain, '(unresolved)'), routing_status
                    ORDER BY source_domain, n DESC, proposed_domain, routing_status
                    """
                )
            ).fetchall()

            stratified_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT source_domain,
                           COALESCE(source_vocabulary_id, '(none)') AS source_vocabulary_id,
                           COALESCE(source_code_type, '(none)') AS source_code_type,
                           COALESCE(proposed_domain, '(unresolved)') AS proposed_domain,
                           routing_status,
                           COUNT_BIG(*) AS n
                    FROM routed
                    GROUP BY source_domain,
                             COALESCE(source_vocabulary_id, '(none)'),
                             COALESCE(source_code_type, '(none)'),
                             COALESCE(proposed_domain, '(unresolved)'),
                             routing_status
                    ORDER BY source_domain, n DESC
                    """
                )
            ).fetchall()

            top_codes = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT TOP (200)
                           source_domain,
                           COALESCE(source_vocabulary_id, '(none)') AS source_vocabulary_id,
                           COALESCE(source_code_type, '(none)') AS source_code_type,
                           condition_source_value,
                           condition_source_concept_id,
                           source_concept_domain,
                           source_standard_concept,
                           source_invalid_reason,
                           standard_target_count,
                           standard_target_domain_count,
                           COALESCE(proposed_domain, '(unresolved)') AS proposed_domain,
                           routing_status,
                           COUNT_BIG(*) AS n
                    FROM routed
                    WHERE condition_concept_id = 0
                    GROUP BY source_domain,
                             COALESCE(source_vocabulary_id, '(none)'),
                             COALESCE(source_code_type, '(none)'),
                             condition_source_value,
                             condition_source_concept_id,
                             source_concept_domain,
                             source_standard_concept,
                             source_invalid_reason,
                             standard_target_count,
                             standard_target_domain_count,
                             COALESCE(proposed_domain, '(unresolved)'),
                             routing_status
                    ORDER BY COUNT_BIG(*) DESC
                    """
                )
            ).mappings().all()

            totals = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT
                      COUNT_BIG(*) AS audited_rows,
                      SUM(CASE WHEN proposed_domain = 'Condition' THEN 1 ELSE 0 END) AS proposed_condition_rows,
                      SUM(CASE WHEN proposed_domain IS NOT NULL AND proposed_domain <> 'Condition' THEN 1 ELSE 0 END) AS cross_domain_rows,
                      SUM(CASE WHEN proposed_domain IS NULL THEN 1 ELSE 0 END) AS unresolved_rows,
                      SUM(CASE WHEN routing_status = 'ambiguous_multiple_target_domains' THEN 1 ELSE 0 END) AS ambiguous_rows
                    FROM routed
                    """
                )
            ).mappings().one()
    finally:
        engine.dispose()

    by_domain = [
        {
            "source_domain": row[0],
            "proposed_domain": row[1],
            "routing_status": row[2],
            "n": int(row[3]),
        }
        for row in domain_rows
    ]
    stratified = [
        {
            "source_domain": row[0],
            "source_vocabulary_id": row[1],
            "source_code_type": row[2],
            "proposed_domain": row[3],
            "routing_status": row[4],
            "n": int(row[5]),
        }
        for row in stratified_rows
    ]

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_domain_routing_audit",
        "read_only": True,
        "routing_rule": {
            "existing_nonzero_condition_concept": "Condition",
            "current_standard_source_concept": "use source concept domain",
            "nonstandard_source_with_one_standard_target_domain": "use that target domain",
            "multiple_standard_target_domains": "ambiguous; do not guess",
            "no_source_or_no_standard_target": "unresolved; preserve source and review",
        },
        "totals": {key: int(value or 0) for key, value in totals.items()},
        "by_source_and_proposed_domain": by_domain,
        "stratified_by_source_vocabulary_code_type_and_domain": stratified,
        "top_200_zero_concept_codes": [dict(row) for row in top_codes],
        "interpretation_note": (
            "This audit quantifies the domain-routing implications of current Athena semantics. "
            "It does not modify condition_occurrence. Results should be reviewed before implementing "
            "cross-domain writes so native PCORnet source domains can later be reconciled without duplication."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    return ConditionDomainRoutingAuditResult(
        audited_rows=int(totals["audited_rows"] or 0),
        proposed_condition_rows=int(totals["proposed_condition_rows"] or 0),
        cross_domain_rows=int(totals["cross_domain_rows"] or 0),
        unresolved_rows=int(totals["unresolved_rows"] or 0),
        ambiguous_rows=int(totals["ambiguous_rows"] or 0),
        audit_path=audit_path,
    )
