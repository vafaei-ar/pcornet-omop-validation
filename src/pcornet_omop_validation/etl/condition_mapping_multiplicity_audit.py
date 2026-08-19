from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

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
            rows = connection.execute(
                text(
                    f"""
                    WITH base AS (
                      SELECT
                        x.source_domain,
                        co.condition_occurrence_id,
                        co.condition_concept_id,
                        co.condition_source_concept_id
                      FROM [{target_schema}].[condition_occurrence] co
                      JOIN [{source_schema}].[etl_condition_occurrence_xwalk] x
                        ON x.condition_occurrence_id = co.condition_occurrence_id
                      WHERE co.condition_concept_id = 0
                        AND co.condition_source_concept_id <> 0
                    ), targets AS (
                      SELECT
                        b.source_domain,
                        b.condition_occurrence_id,
                        COUNT(DISTINCT tgt.concept_id) AS target_concept_count,
                        COUNT(DISTINCT tgt.domain_id) AS target_domain_count
                      FROM base b
                      JOIN [{target_schema}].[concept_relationship] cr
                        ON cr.concept_id_1 = b.condition_source_concept_id
                       AND cr.relationship_id = 'Maps to'
                       AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                      JOIN [{target_schema}].[concept] tgt
                        ON tgt.concept_id = cr.concept_id_2
                       AND tgt.standard_concept = 'S'
                       AND tgt.invalid_reason IS NULL
                      GROUP BY b.source_domain, b.condition_occurrence_id
                    )
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
        "interpretation_note": (
            "Rows with more than one active standard target concept require explicit split-mapping handling; "
            "a unique target domain alone is not sufficient to select one standard concept."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Single standard target rows: {single_target:,}")
    print(f"Multiple standard target concept rows: {multi_concept:,}")
    print(f"Multiple standard target domain rows: {multi_domain:,}")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
