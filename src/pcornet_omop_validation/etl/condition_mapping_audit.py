from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


@dataclass(frozen=True)
class ConditionMappingAuditResult:
    audited_rows: int
    zero_rows: int
    diagnosis_zero_rows: int
    condition_zero_rows: int
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


def audit_condition_mapping(config: EtlConfig) -> ConditionMappingAuditResult:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_mapping_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            base_cte = f"""
            WITH base AS (
              SELECT
                x.source_domain,
                x.source_code_type,
                co.condition_occurrence_id,
                co.condition_source_value,
                co.condition_source_concept_id,
                co.condition_concept_id,
                src.vocabulary_id AS source_vocabulary_id,
                src.domain_id AS source_domain_id,
                src.standard_concept AS source_standard_concept,
                src.invalid_reason AS source_invalid_reason,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{target_schema}].[concept_relationship] cr
                    WHERE cr.concept_id_1 = co.condition_source_concept_id
                      AND cr.relationship_id = 'Maps to'
                      AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                ) THEN 1 ELSE 0 END AS has_active_maps_to,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{target_schema}].[concept_relationship] cr
                    JOIN [{target_schema}].[concept] tgt
                      ON tgt.concept_id = cr.concept_id_2
                    WHERE cr.concept_id_1 = co.condition_source_concept_id
                      AND cr.relationship_id = 'Maps to'
                      AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                      AND tgt.standard_concept = 'S'
                      AND tgt.invalid_reason IS NULL
                ) THEN 1 ELSE 0 END AS has_active_standard_target,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM [{target_schema}].[concept_relationship] cr
                    JOIN [{target_schema}].[concept] tgt
                      ON tgt.concept_id = cr.concept_id_2
                    WHERE cr.concept_id_1 = co.condition_source_concept_id
                      AND cr.relationship_id = 'Maps to'
                      AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                      AND tgt.standard_concept = 'S'
                      AND tgt.invalid_reason IS NULL
                      AND tgt.domain_id = 'Condition'
                ) THEN 1 ELSE 0 END AS has_active_standard_condition_target,
                CASE WHEN (
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[concept_relationship] cr
                    JOIN [{target_schema}].[concept] tgt
                      ON tgt.concept_id = cr.concept_id_2
                    WHERE cr.concept_id_1 = co.condition_source_concept_id
                      AND cr.relationship_id = 'Maps to'
                      AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                      AND tgt.standard_concept = 'S'
                      AND tgt.invalid_reason IS NULL
                      AND tgt.domain_id = 'Condition'
                ) > 1 THEN 1 ELSE 0 END AS multiple_standard_condition_targets
              FROM [{target_schema}].[condition_occurrence] co
              JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                ON x.condition_occurrence_id = co.condition_occurrence_id
              LEFT JOIN [{target_schema}].[concept] src
                ON src.concept_id = co.condition_source_concept_id
            ), classified AS (
              SELECT *,
                CASE
                  WHEN condition_concept_id <> 0 THEN 'mapped_successfully'
                  WHEN condition_source_concept_id = 0 THEN 'source_concept_not_found'
                  WHEN source_invalid_reason IS NOT NULL THEN 'source_concept_invalid'
                  WHEN source_standard_concept = 'S' AND source_domain_id = 'Condition' THEN 'standard_condition_not_carried_forward'
                  WHEN multiple_standard_condition_targets = 1 THEN 'ambiguous_multiple_condition_targets'
                  WHEN has_active_standard_condition_target = 1 THEN 'condition_target_available_but_not_used'
                  WHEN has_active_standard_target = 1 THEN 'maps_to_standard_other_domain_only'
                  WHEN has_active_maps_to = 1 THEN 'maps_to_nonstandard_or_invalid_target_only'
                  ELSE 'no_active_maps_to'
                END AS mapping_outcome
              FROM base
            )
            """

            summary_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT source_domain, COALESCE(source_vocabulary_id, '(none)') AS vocabulary_id,
                           COALESCE(source_code_type, '(none)') AS source_code_type,
                           mapping_outcome, COUNT_BIG(*) AS n
                    FROM classified
                    GROUP BY source_domain, COALESCE(source_vocabulary_id, '(none)'),
                             COALESCE(source_code_type, '(none)'), mapping_outcome
                    ORDER BY source_domain, vocabulary_id, source_code_type, mapping_outcome
                    """
                )
            ).fetchall()

            overall_rows = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT source_domain, mapping_outcome, COUNT_BIG(*) AS n
                    FROM classified
                    GROUP BY source_domain, mapping_outcome
                    ORDER BY source_domain, mapping_outcome
                    """
                )
            ).fetchall()

            top_unmapped = connection.execute(
                text(
                    base_cte
                    + """
                    SELECT TOP (100)
                           source_domain,
                           COALESCE(source_vocabulary_id, '(none)') AS vocabulary_id,
                           COALESCE(source_code_type, '(none)') AS source_code_type,
                           condition_source_value,
                           condition_source_concept_id,
                           source_domain_id,
                           source_standard_concept,
                           source_invalid_reason,
                           mapping_outcome,
                           COUNT_BIG(*) AS n
                    FROM classified
                    WHERE condition_concept_id = 0
                    GROUP BY source_domain, COALESCE(source_vocabulary_id, '(none)'),
                             COALESCE(source_code_type, '(none)'), condition_source_value,
                             condition_source_concept_id, source_domain_id,
                             source_standard_concept, source_invalid_reason, mapping_outcome
                    ORDER BY COUNT_BIG(*) DESC
                    """
                )
            ).mappings().all()

            totals = connection.execute(
                text(
                    f"""
                    SELECT
                      COUNT_BIG(*) AS audited_rows,
                      SUM(CASE WHEN co.condition_concept_id = 0 THEN 1 ELSE 0 END) AS zero_rows,
                      SUM(CASE WHEN x.source_domain='DIAGNOSIS' AND co.condition_concept_id=0 THEN 1 ELSE 0 END) AS diagnosis_zero_rows,
                      SUM(CASE WHEN x.source_domain='CONDITION' AND co.condition_concept_id=0 THEN 1 ELSE 0 END) AS condition_zero_rows
                    FROM [{target_schema}].[condition_occurrence] co
                    JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                      ON x.condition_occurrence_id = co.condition_occurrence_id
                    """
                )
            ).mappings().one()
    finally:
        engine.dispose()

    stratified = [
        {
            "source_domain": row[0],
            "vocabulary_id": row[1],
            "source_code_type": row[2],
            "mapping_outcome": row[3],
            "n": int(row[4]),
        }
        for row in summary_rows
    ]
    overall = [
        {"source_domain": row[0], "mapping_outcome": row[1], "n": int(row[2])}
        for row in overall_rows
    ]

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_mapping_audit",
        "classification_order": [
            "mapped_successfully",
            "source_concept_not_found",
            "source_concept_invalid",
            "standard_condition_not_carried_forward",
            "ambiguous_multiple_condition_targets",
            "condition_target_available_but_not_used",
            "maps_to_standard_other_domain_only",
            "maps_to_nonstandard_or_invalid_target_only",
            "no_active_maps_to",
        ],
        "totals": {key: int(value or 0) for key, value in totals.items()},
        "overall_by_source": overall,
        "stratified_by_source_vocabulary_and_code_type": stratified,
        "top_100_unmapped_codes": [dict(row) for row in top_unmapped],
        "interpretation_note": (
            "This audit does not modify condition_occurrence. It decomposes concept_id=0 outcomes "
            "against the vocabulary loaded in OMOP_VALIDATED so mapping policy can be reviewed before rerunning the ETL."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    return ConditionMappingAuditResult(
        audited_rows=int(totals["audited_rows"] or 0),
        zero_rows=int(totals["zero_rows"] or 0),
        diagnosis_zero_rows=int(totals["diagnosis_zero_rows"] or 0),
        condition_zero_rows=int(totals["condition_zero_rows"] or 0),
        audit_path=audit_path,
    )
