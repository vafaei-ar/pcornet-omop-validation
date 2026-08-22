from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine
from .condition_occurrence import _eligible_ctes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit multiplicity of exact source-code concept matches used by "
            "the Condition primary ETL before standard mapping."
        )
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    config = load_etl_config(args.config)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_source_concept_multiplicity_audit.json"

    cte = _eligible_ctes(source_schema, target_schema)
    sql = cte + f"""
    , events AS (
      SELECT CAST(DX AS nvarchar(255)) AS source_code, vocabulary_id
      FROM diag_eligible
      UNION ALL
      SELECT CAST(CONDITION AS nvarchar(255)), vocabulary_id
      FROM cond_eligible
    ), code_keys AS (
      SELECT source_code, vocabulary_id, COUNT_BIG(*) AS event_rows
      FROM events
      WHERE source_code IS NOT NULL AND vocabulary_id IS NOT NULL
      GROUP BY source_code, vocabulary_id
    ), candidates AS (
      SELECT
        k.source_code,
        k.vocabulary_id,
        k.event_rows,
        COUNT(c.concept_id) AS total_candidate_count,
        SUM(CASE WHEN c.invalid_reason IS NULL THEN 1 ELSE 0 END) AS active_candidate_count
      FROM code_keys k
      LEFT JOIN [{target_schema}].[concept] c
        ON c.concept_code = k.source_code
       AND c.vocabulary_id = k.vocabulary_id
      GROUP BY k.source_code, k.vocabulary_id, k.event_rows
    )
    SELECT
      CASE
        WHEN total_candidate_count = 0 THEN 'no_candidate'
        WHEN active_candidate_count = 1 THEN 'unique_active_candidate'
        WHEN active_candidate_count > 1 THEN 'multiple_active_candidates'
        WHEN total_candidate_count = 1 THEN 'unique_invalid_candidate'
        ELSE 'multiple_invalid_candidates_no_active'
      END AS lookup_class,
      SUM(event_rows) AS event_rows,
      COUNT_BIG(*) AS code_keys,
      MAX(total_candidate_count) AS max_total_candidates,
      MAX(active_candidate_count) AS max_active_candidates
    FROM candidates
    GROUP BY
      CASE
        WHEN total_candidate_count = 0 THEN 'no_candidate'
        WHEN active_candidate_count = 1 THEN 'unique_active_candidate'
        WHEN active_candidate_count > 1 THEN 'multiple_active_candidates'
        WHEN total_candidate_count = 1 THEN 'unique_invalid_candidate'
        ELSE 'multiple_invalid_candidates_no_active'
      END
    ORDER BY lookup_class
    """

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
    finally:
        engine.dispose()

    detail = [
        {
            "lookup_class": row[0],
            "event_rows": int(row[1] or 0),
            "code_keys": int(row[2] or 0),
            "max_total_candidates": int(row[3] or 0),
            "max_active_candidates": int(row[4] or 0),
        }
        for row in rows
    ]

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_source_concept_multiplicity_audit",
        "read_only": True,
        "policy_question": (
            "Whether exact source code plus vocabulary identifies a unique source "
            "concept without arbitrary TOP (1) selection."
        ),
        "classes": detail,
        "interpretation": {
            "unique_active_candidate": "deterministic active exact source concept",
            "unique_invalid_candidate": (
                "deterministic exact source concept, but invalid/retired; may still "
                "serve as lineage and Maps to source"
            ),
            "multiple_active_candidates": (
                "ambiguous exact lookup; primary ETL must not choose one arbitrarily"
            ),
            "multiple_invalid_candidates_no_active": (
                "ambiguous exact lookup among retired concepts; primary ETL must not "
                "choose one arbitrarily"
            ),
            "no_candidate": "no exact vocabulary concept exists for the source code",
        },
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    for row in detail:
        print(
            f"{row['lookup_class']}: event_rows={row['event_rows']:,} "
            f"code_keys={row['code_keys']:,} "
            f"max_total={row['max_total_candidates']:,} "
            f"max_active={row['max_active_candidates']:,}"
        )
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
