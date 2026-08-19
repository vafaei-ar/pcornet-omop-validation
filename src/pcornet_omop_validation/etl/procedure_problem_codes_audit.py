from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


BASE_CTES = """
WITH eligible AS (
  SELECT
    PROCEDURESID,
    LTRIM(RTRIM(CAST(PX AS nvarchar(255)))) AS px,
    UPPER(LTRIM(RTRIM(CAST(PX_TYPE AS nvarchar(20))))) AS px_type,
    NULLIF(LTRIM(RTRIM(CAST(RAW_PX AS nvarchar(255)))), '') AS raw_px,
    NULLIF(UPPER(LTRIM(RTRIM(CAST(RAW_PX_TYPE AS nvarchar(100))))), '') AS raw_px_type
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
    c.concept_name AS source_concept_name,
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
    tgt.concept_id AS target_concept_id,
    tgt.concept_name AS target_concept_name,
    tgt.vocabulary_id AS target_vocabulary_id,
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
    MAX(CASE WHEN mt.target_domain_id = 'Procedure' THEN 1 ELSE 0 END) AS has_procedure_target
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
    us.source_concept_name,
    us.vocabulary_id,
    us.source_domain_id,
    us.source_standard_concept,
    us.source_invalid_reason,
    COALESCE(ts.target_concept_count, 0) AS target_concept_count,
    COALESCE(ts.target_domain_count, 0) AS target_domain_count,
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
"""


def _rows_to_dicts(rows) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit unresolved, ambiguous, and unsupported native PCORnet PROCEDURES codes "
            "before materializing the procedure routing ledger."
        )
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_problem_codes_audit.json"

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

            ctes = BASE_CTES.format(
                source_schema=source_schema,
                target_schema=target_schema,
            )

            problem_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      px_type, px, source_rows, mapping_outcome,
                      source_concept_count, source_concept_id, source_concept_name,
                      vocabulary_id, source_domain_id, source_standard_concept,
                      source_invalid_reason, target_concept_count, target_domain_count
                    FROM classified
                    WHERE mapping_outcome IN (
                      'source_concept_not_found',
                      'ambiguous_source_concept',
                      'no_active_standard_target',
                      'maps_to_multiple_domains',
                      'unsupported_px_type'
                    )
                    ORDER BY source_rows DESC, px_type, px
                    """
                )
            ).mappings().all()

            candidate_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      sc.px_type, sc.px,
                      c.mapping_outcome,
                      sc.source_rows,
                      sc.source_concept_id,
                      sc.source_concept_name,
                      sc.vocabulary_id,
                      sc.source_domain_id,
                      sc.source_standard_concept,
                      sc.source_invalid_reason
                    FROM source_candidates sc
                    JOIN classified c
                      ON c.px_type = sc.px_type
                     AND c.px = sc.px
                    WHERE c.mapping_outcome IN (
                      'ambiguous_source_concept',
                      'no_active_standard_target',
                      'maps_to_multiple_domains'
                    )
                      AND sc.source_concept_id IS NOT NULL
                    ORDER BY sc.px_type, sc.px, sc.vocabulary_id, sc.source_concept_id
                    """
                )
            ).mappings().all()

            target_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      mt.px_type, mt.px,
                      c.mapping_outcome,
                      mt.source_rows,
                      mt.source_concept_id,
                      mt.target_concept_id,
                      mt.target_concept_name,
                      mt.target_vocabulary_id,
                      mt.target_domain_id
                    FROM mapped_targets mt
                    JOIN classified c
                      ON c.px_type = mt.px_type
                     AND c.px = mt.px
                    WHERE c.mapping_outcome IN (
                      'no_active_standard_target',
                      'maps_to_multiple_domains'
                    )
                    ORDER BY mt.px_type, mt.px, mt.target_domain_id, mt.target_concept_id
                    """
                )
            ).mappings().all()

            raw_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      e.px_type,
                      e.px,
                      c.mapping_outcome,
                      e.raw_px_type,
                      e.raw_px,
                      COUNT_BIG(*) AS source_rows
                    FROM eligible e
                    JOIN classified c
                      ON c.px_type = e.px_type
                     AND c.px = e.px
                    WHERE c.mapping_outcome IN (
                      'source_concept_not_found',
                      'ambiguous_source_concept',
                      'no_active_standard_target',
                      'maps_to_multiple_domains',
                      'unsupported_px_type'
                    )
                    GROUP BY e.px_type, e.px, c.mapping_outcome, e.raw_px_type, e.raw_px
                    ORDER BY e.px_type, e.px, COUNT_BIG(*) DESC, e.raw_px_type, e.raw_px
                    """
                )
            ).mappings().all()

            ot_raw_type_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      COALESCE(raw_px_type, '(missing)') AS raw_px_type,
                      COUNT_BIG(*) AS source_rows,
                      COUNT(DISTINCT px) AS distinct_px_codes,
                      COUNT(DISTINCT raw_px) AS distinct_raw_px_codes
                    FROM eligible
                    WHERE px_type = 'OT'
                    GROUP BY COALESCE(raw_px_type, '(missing)')
                    ORDER BY COUNT_BIG(*) DESC, raw_px_type
                    """
                )
            ).mappings().all()

            summary_rows = connection.execute(
                text(
                    ctes
                    + """
                    SELECT
                      px_type,
                      mapping_outcome,
                      SUM(source_rows) AS source_rows,
                      COUNT_BIG(*) AS distinct_codes
                    FROM classified
                    WHERE mapping_outcome IN (
                      'source_concept_not_found',
                      'ambiguous_source_concept',
                      'no_active_standard_target',
                      'maps_to_multiple_domains',
                      'unsupported_px_type'
                    )
                    GROUP BY px_type, mapping_outcome
                    ORDER BY px_type, source_rows DESC, mapping_outcome
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "procedure_problem_codes_audit",
        "source_table": f"{source_schema}.PCORnet_PROCEDURES",
        "summary": _rows_to_dicts(summary_rows),
        "problem_codes": _rows_to_dicts(problem_rows),
        "source_concept_candidates": _rows_to_dicts(candidate_rows),
        "standard_targets_for_problem_codes": _rows_to_dicts(target_rows),
        "raw_source_variants": _rows_to_dicts(raw_rows),
        "ot_raw_px_type_distribution": _rows_to_dicts(ot_raw_type_rows),
        "interpretation_note": (
            "Read-only diagnostic. PX_TYPE constrains source vocabulary for CH/09/10. "
            "OT is not mapped by guessing a vocabulary. RAW_PX_TYPE and RAW_PX are reported only "
            "to determine whether a defensible source-system rule can be specified. All active "
            "standard Maps to targets are retained so multi-domain and one-to-many mappings remain visible."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    print("Problematic native PROCEDURES codes:")
    for row in summary_rows:
        print(
            f"  PX_TYPE={row['px_type']:>2s}  {row['mapping_outcome']:32s} "
            f"rows={int(row['source_rows']):,} codes={int(row['distinct_codes']):,}"
        )
    print("OT RAW_PX_TYPE distribution:")
    for row in ot_raw_type_rows:
        print(
            f"  {row['raw_px_type']}: rows={int(row['source_rows']):,} "
            f"PX={int(row['distinct_px_codes']):,} RAW_PX={int(row['distinct_raw_px_codes']):,}"
        )
    print(f"Detailed problem codes: {len(problem_rows):,}")
    print(f"Detailed source candidates: {len(candidate_rows):,}")
    print(f"Detailed standard targets: {len(target_rows):,}")
    print(f"RAW source variants: {len(raw_rows):,}")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
