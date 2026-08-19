from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit multiplicity of active standard Maps to targets for condition-derived events."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_mapping_multiplicity_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            base_sql = f"""
            WITH base AS (
              SELECT
                x.source_domain,
                co.condition_occurrence_id,
                co.condition_source_concept_id
              FROM [{target_schema}].[condition_occurrence] co
              JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                ON x.condition_occurrence_id = co.condition_occurrence_id
              WHERE co.condition_concept_id = 0
                AND co.condition_source_concept_id <> 0
            ), target_rows AS (
              SELECT DISTINCT
                b.source_domain,
                b.condition_occurrence_id,
                tgt.concept_id AS target_concept_id,
                tgt.domain_id AS target_domain
              FROM base b
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = b.condition_source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.invalid_reason IS NULL
            ), targets AS (
              SELECT
                source_domain,
                condition_occurrence_id,
                COUNT(DISTINCT target_concept_id) AS target_concept_count,
                COUNT(DISTINCT target_domain) AS target_domain_count
              FROM target_rows
              GROUP BY source_domain, condition_occurrence_id
            )
            """

            rows = connection.execute(
                text(
                    base_sql
                    + """
                    SELECT
                      source_domain,
                      target_concept_count,
                      target_domain_count,
                      COUNT_BIG(*) AS n
                    FROM targets
                    GROUP BY source_domain, target_concept_count, target_domain_count
                    ORDER BY source_domain, target_concept_count, target_domain_count
                    """
                )
            ).fetchall()

            by_domain_rows = connection.execute(
                text(
                    base_sql
                    + """
                    , event_domain AS (
                      SELECT
                        tr.source_domain,
                        tr.condition_occurrence_id,
                        tr.target_domain,
                        t.target_concept_count,
                        t.target_domain_count,
                        COUNT(DISTINCT tr.target_concept_id) AS concepts_in_domain
                      FROM target_rows tr
                      JOIN targets t
                        ON t.source_domain = tr.source_domain
                       AND t.condition_occurrence_id = tr.condition_occurrence_id
                      GROUP BY tr.source_domain, tr.condition_occurrence_id, tr.target_domain,
                               t.target_concept_count, t.target_domain_count
                    )
                    SELECT
                      source_domain,
                      target_domain,
                      target_concept_count,
                      target_domain_count,
                      concepts_in_domain,
                      COUNT_BIG(*) AS source_event_rows
                    FROM event_domain
                    GROUP BY source_domain, target_domain, target_concept_count,
                             target_domain_count, concepts_in_domain
                    ORDER BY source_domain, target_domain, target_concept_count,
                             target_domain_count, concepts_in_domain
                    """
                )
            ).fetchall()

            output_rows = connection.execute(
                text(
                    base_sql
                    + """
                    SELECT
                      tr.source_domain,
                      tr.target_domain,
                      COUNT_BIG(*) AS output_target_rows,
                      COUNT(DISTINCT tr.condition_occurrence_id) AS source_event_rows
                    FROM target_rows tr
                    GROUP BY tr.source_domain, tr.target_domain
                    ORDER BY tr.source_domain, output_target_rows DESC, tr.target_domain
                    """
                )
            ).fetchall()
    finally:
        engine.dispose()

    detail = [
        {
            "source_domain": row[0],
            "target_concept_count": int(row[1]),
            "target_domain_count": int(row[2]),
            "n": int(row[3]),
        }
        for row in rows
    ]
    by_target_domain = [
        {
            "source_domain": row[0],
            "target_domain": row[1],
            "target_concept_count": int(row[2]),
            "target_domain_count": int(row[3]),
            "concepts_in_domain": int(row[4]),
            "source_event_rows": int(row[5]),
        }
        for row in by_domain_rows
    ]
    expected_outputs_by_domain = [
        {
            "source_domain": row[0],
            "target_domain": row[1],
            "output_target_rows": int(row[2]),
            "source_event_rows": int(row[3]),
        }
        for row in output_rows
    ]

    multi_concept = sum(r["n"] for r in detail if r["target_concept_count"] > 1)
    multi_domain = sum(r["n"] for r in detail if r["target_domain_count"] > 1)
    single_target = sum(r["n"] for r in detail if r["target_concept_count"] == 1)

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_mapping_multiplicity_audit",
        "single_standard_target_rows": single_target,
        "multiple_standard_target_concept_rows": multi_concept,
        "multiple_standard_target_domain_rows": multi_domain,
        "distribution": detail,
        "multiplicity_by_target_domain": by_target_domain,
        "expected_output_rows_by_target_domain": expected_outputs_by_domain,
        "interpretation_note": (
            "Rows with more than one active standard target concept require explicit split-mapping handling. "
            "expected_output_rows_by_target_domain counts target concept rows after expansion, whereas "
            "source_event_rows counts distinct source events contributing to each domain."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Single standard target rows: {single_target:,}")
    print(f"Multiple standard target concept rows: {multi_concept:,}")
    print(f"Multiple standard target domain rows: {multi_domain:,}")
    print("Expected expanded outputs by target domain:")
    for row in expected_outputs_by_domain:
        print(
            f"  {row['source_domain']:10s} -> {row['target_domain']:20s} "
            f"source_events={row['source_event_rows']:,} "
            f"target_rows={row['output_target_rows']:,}"
        )
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
