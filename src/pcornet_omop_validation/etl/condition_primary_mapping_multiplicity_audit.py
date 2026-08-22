from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


def _vocabulary_case(column: str) -> str:
    return f"""
    CASE UPPER(LTRIM(RTRIM(CAST({column} AS nvarchar(50)))))
      WHEN '09' THEN 'ICD9CM'
      WHEN '9' THEN 'ICD9CM'
      WHEN 'ICD9' THEN 'ICD9CM'
      WHEN 'ICD9CM' THEN 'ICD9CM'
      WHEN '10' THEN 'ICD10CM'
      WHEN 'ICD10' THEN 'ICD10CM'
      WHEN 'ICD10CM' THEN 'ICD10CM'
      WHEN 'SM' THEN 'SNOMED'
      WHEN 'SNOMED' THEN 'SNOMED'
      WHEN 'SNOMEDCT' THEN 'SNOMED'
      ELSE NULL
    END
    """.strip()


def audit_condition_primary_mapping_multiplicity(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_primary_mapping_multiplicity_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_DIAGNOSIS"),
                (source_schema, "PCORnet_CONDITION"),
                (target_schema, "person"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            dx_vocab = _vocabulary_case("d.DX_TYPE")
            condition_vocab = _vocabulary_case("c.CONDITION_TYPE")

            cte = f"""
            WITH eligible AS (
              SELECT
                CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
                CAST(d.DIAGNOSISID AS nvarchar(255)) AS source_record_id,
                CAST(d.DX AS nvarchar(255)) AS source_code,
                {dx_vocab} AS vocabulary_id
              FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
              JOIN [{target_schema}].[person] p
                ON CAST(d.PATID AS nvarchar(50)) = p.person_source_value
              WHERE d.DIAGNOSISID IS NOT NULL
                AND LTRIM(RTRIM(CAST(d.DIAGNOSISID AS nvarchar(max)))) <> ''
                AND d.DX_DATE IS NOT NULL

              UNION ALL

              SELECT
                CAST('CONDITION' AS varchar(16)),
                CAST(c.CONDITIONID AS nvarchar(255)),
                CAST(c.CONDITION AS nvarchar(255)),
                {condition_vocab}
              FROM [{source_schema}].[PCORnet_CONDITION] c
              JOIN [{target_schema}].[person] p
                ON CAST(c.PATID AS nvarchar(50)) = p.person_source_value
              WHERE c.CONDITIONID IS NOT NULL
                AND LTRIM(RTRIM(CAST(c.CONDITIONID AS nvarchar(max)))) <> ''
                AND COALESCE(c.ONSET_DATE, c.REPORT_DATE) IS NOT NULL
                AND (
                  c.RESOLVE_DATE IS NULL
                  OR CAST(c.RESOLVE_DATE AS date) >=
                     CAST(COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS date)
                )
            ),
            source_concepts AS (
              SELECT
                e.source_domain,
                e.source_record_id,
                e.source_code,
                e.vocabulary_id,
                src.concept_id AS source_concept_id,
                src.domain_id AS source_domain_id,
                src.standard_concept AS source_standard_concept,
                src.invalid_reason AS source_invalid_reason
              FROM eligible e
              OUTER APPLY (
                SELECT TOP (1)
                  c.concept_id,
                  c.domain_id,
                  c.standard_concept,
                  c.invalid_reason
                FROM [{target_schema}].[concept] c
                WHERE c.concept_code = e.source_code
                  AND c.vocabulary_id = e.vocabulary_id
                ORDER BY
                  CASE WHEN c.invalid_reason IS NULL THEN 0 ELSE 1 END,
                  c.concept_id
              ) src
            ),
            mapped_condition_targets AS (
              SELECT DISTINCT
                s.source_domain,
                s.source_record_id,
                tgt.concept_id AS target_concept_id
              FROM source_concepts s
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = s.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.domain_id = 'Condition'
               AND tgt.invalid_reason IS NULL
              WHERE NOT (
                s.source_concept_id IS NOT NULL
                AND s.source_invalid_reason IS NULL
                AND COALESCE(s.source_standard_concept, '') = 'S'
                AND s.source_domain_id = 'Condition'
              )
            ),
            multiplicity AS (
              SELECT
                s.source_domain,
                s.source_record_id,
                CASE
                  WHEN s.source_concept_id IS NOT NULL
                   AND s.source_invalid_reason IS NULL
                   AND COALESCE(s.source_standard_concept, '') = 'S'
                   AND s.source_domain_id = 'Condition'
                    THEN 1
                  ELSE COUNT(DISTINCT m.target_concept_id)
                END AS condition_target_count
              FROM source_concepts s
              LEFT JOIN mapped_condition_targets m
                ON m.source_domain = s.source_domain
               AND m.source_record_id = s.source_record_id
              GROUP BY
                s.source_domain,
                s.source_record_id,
                s.source_concept_id,
                s.source_invalid_reason,
                s.source_standard_concept,
                s.source_domain_id
            )
            """

            distribution_rows = con.execute(
                text(
                    cte
                    + """
                    SELECT
                      source_domain,
                      condition_target_count,
                      COUNT_BIG(*) AS source_events
                    FROM multiplicity
                    GROUP BY source_domain, condition_target_count
                    ORDER BY source_domain, condition_target_count
                    """
                )
            ).fetchall()

            totals = con.execute(
                text(
                    cte
                    + """
                    SELECT
                      COUNT_BIG(*) AS eligible_events,
                      SUM(CASE WHEN condition_target_count = 0 THEN 1 ELSE 0 END) AS zero_condition_target_events,
                      SUM(CASE WHEN condition_target_count = 1 THEN 1 ELSE 0 END) AS unique_condition_target_events,
                      SUM(CASE WHEN condition_target_count > 1 THEN 1 ELSE 0 END) AS multiple_condition_target_events,
                      COALESCE(MAX(condition_target_count), 0) AS max_condition_targets
                    FROM multiplicity
                    """
                )
            ).mappings().one()

            top_multiple = con.execute(
                text(
                    cte
                    + """
                    SELECT TOP (50)
                      m.source_domain,
                      m.source_record_id,
                      s.source_code,
                      s.vocabulary_id,
                      s.source_concept_id,
                      m.condition_target_count
                    FROM multiplicity m
                    JOIN source_concepts s
                      ON s.source_domain = m.source_domain
                     AND s.source_record_id = m.source_record_id
                    WHERE m.condition_target_count > 1
                    ORDER BY m.condition_target_count DESC, m.source_domain, m.source_record_id
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_primary_mapping_multiplicity_audit",
        "read_only": True,
        "policy_under_review": (
            "Primary Condition mapping must never choose an arbitrary target when more than one "
            "active Standard Condition concept is available."
        ),
        "totals": {key: int(value or 0) for key, value in totals.items()},
        "distribution": [
            {
                "source_domain": row[0],
                "condition_target_count": int(row[1]),
                "source_events": int(row[2]),
            }
            for row in distribution_rows
        ],
        "top_multiple_target_events": [dict(row) for row in top_multiple],
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return {**payload, "audit_path": str(audit_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit multiplicity of active Standard Condition targets used by primary Condition ETL."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)
    result = audit_condition_primary_mapping_multiplicity(config)
    totals = result["totals"]
    print(f"Eligible source events: {totals['eligible_events']:,}")
    print(f"Zero Condition targets: {totals['zero_condition_target_events']:,}")
    print(f"Unique Condition target: {totals['unique_condition_target_events']:,}")
    print(f"Multiple Condition targets: {totals['multiple_condition_target_events']:,}")
    print(f"Maximum Condition targets per event: {totals['max_condition_targets']:,}")
    print(f"Audit: {result['audit_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
