from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_observation_xwalk"


def audit_observation_dynamic_reconciliation(config: EtlConfig) -> dict:
    """Audit Observation using source-derived, site-independent expectations.

    No dataset-specific row counts are encoded. Expected counts are derived
    from PCORnet source tables plus the prespecified OBS_CLIN and PROCEDURES
    domain-route ledgers, then compared with the materialized OMOP table and
    lineage table.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "observation_dynamic_reconciliation.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "etl_obs_clin_route"),
                (source_schema, "etl_procedure_event_route"),
                (source_schema, "PCORnet_OBS_GEN"),
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (source_schema, "PCORnet_VITAL"),
                (source_schema, XWALK_TABLE),
                (target_schema, "observation"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            expected = {
                "OBS_CLIN": int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[etl_obs_clin_route]
                            WHERE target_domain = 'Observation'
                        """)
                    ).scalar_one()
                ),
                "OBS_GEN": int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[PCORnet_OBS_GEN]
                        """)
                    ).scalar_one()
                ),
                "LAB_RESULT_CM": int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                            JOIN [{target_schema}].[concept] c
                              ON c.vocabulary_id = 'LOINC'
                             AND c.concept_code =
                                 LTRIM(RTRIM(CONVERT(
                                   nvarchar(255), l.LAB_LOINC
                                 )))
                             AND c.standard_concept = 'S'
                             AND c.invalid_reason IS NULL
                             AND c.domain_id = 'Observation'
                        """)
                    ).scalar_one()
                ),
                "PROCEDURES": int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[etl_procedure_event_route]
                            WHERE target_domain = 'Observation'
                        """)
                    ).scalar_one()
                ),
                "VITAL": int(
                    con.execute(
                        text(f"""
                            SELECT
                                COUNT_BIG(SMOKING)
                              + COUNT_BIG(TOBACCO)
                              + COUNT_BIG(TOBACCO_TYPE)
                            FROM [{source_schema}].[PCORnet_VITAL]
                        """)
                    ).scalar_one()
                ),
            }
            expected_total = sum(expected.values())

            actual_by_family = {
                str(r[0]): int(r[1])
                for r in con.execute(
                    text(f"""
                        SELECT x.source_family, COUNT_BIG(*)
                        FROM [{target_schema}].[observation] o
                        JOIN [{source_schema}].[{XWALK_TABLE}] x
                          ON x.observation_id = o.observation_id
                        GROUP BY x.source_family
                    """)
                ).fetchall()
            }

            target_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[observation]
                    """)
                ).scalar_one()
            )
            lineage_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[{XWALK_TABLE}]
                    """)
                ).scalar_one()
            )

            expected_concept_zero = int(
                expected["OBS_GEN"]
                + con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[etl_obs_clin_route]
                        WHERE target_domain = 'Observation'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
                + con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[etl_procedure_event_route]
                        WHERE target_domain = 'Observation'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
            )
            actual_concept_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[observation]
                        WHERE observation_concept_id = 0
                    """)
                ).scalar_one()
            )

            expected_vital_value_zero = int(
                con.execute(
                    text(f"""
                        SELECT
                            SUM(CASE
                                  WHEN SMOKING IS NOT NULL
                                   AND SMOKING NOT IN (
                                     '01','02','03','04','05','06','07','08'
                                   )
                                  THEN 1 ELSE 0 END)
                          + COUNT_BIG(TOBACCO)
                          + SUM(CASE
                                  WHEN TOBACCO_TYPE IS NOT NULL
                                   AND TOBACCO_TYPE NOT IN ('01','03','05')
                                  THEN 1 ELSE 0 END)
                        FROM [{source_schema}].[PCORnet_VITAL]
                    """)
                ).scalar_one()
            )
            actual_vital_value_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[observation] o
                        JOIN [{source_schema}].[{XWALK_TABLE}] x
                          ON x.observation_id = o.observation_id
                        WHERE x.source_family = 'VITAL'
                          AND o.value_as_concept_id = 0
                    """)
                ).scalar_one()
            )

            family_mismatches = {
                family: {
                    "expected": count,
                    "actual": actual_by_family.get(family, 0),
                }
                for family, count in expected.items()
                if actual_by_family.get(family, 0) != count
            }

            status = "matched"
            if (
                family_mismatches
                or target_rows != expected_total
                or lineage_rows != expected_total
                or actual_concept_zero != expected_concept_zero
                or actual_vital_value_zero != expected_vital_value_zero
            ):
                status = "review_required"

    finally:
        engine.dispose()

    payload = {
        "stage": "observation_dynamic_reconciliation_audit",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "rule": (
            "All expected Observation counts are derived from source tables, "
            "route ledgers, and prespecified semantic mappings; no site-specific "
            "row-count constants are used."
        ),
        "expected_by_family": expected,
        "actual_by_family": actual_by_family,
        "family_mismatches": family_mismatches,
        "expected_total_rows": expected_total,
        "target_rows": target_rows,
        "lineage_rows": lineage_rows,
        "expected_observation_concept_zero_rows": expected_concept_zero,
        "actual_observation_concept_zero_rows": actual_concept_zero,
        "expected_vital_value_concept_zero_rows": expected_vital_value_zero,
        "actual_vital_value_concept_zero_rows": actual_vital_value_zero,
        "status": status,
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload
