from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def audit_measurement_lab_mapping(config: EtlConfig) -> dict[str, object]:
    """Audit LAB_LOINC source-concept and Measurement-target multiplicity.

    This audit is read-only. It evaluates a generalizable mapping policy:
    an exact LOINC source concept is used only when unique; an active Standard
    Measurement concept maps directly; a nonstandard/invalid source concept
    maps only when it has exactly one active Standard Measurement target;
    active Standard concepts in another domain route outside Measurement;
    all remaining LAB rows stay in Measurement with concept_id=0 rather than
    being dropped or assigned an arbitrary vocabulary target.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "measurement_lab_mapping_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            for schema, table in (
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            ):
                if not table_exists(connection, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]",
            )

            cte = f"""
            WITH lab_codes AS (
              SELECT DISTINCT
                NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), LAB_LOINC))), '') AS loinc
              FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
            ),
            source_candidates AS (
              SELECT
                lc.loinc,
                c.concept_id,
                c.domain_id,
                c.standard_concept,
                c.invalid_reason
              FROM lab_codes lc
              LEFT JOIN [{target_schema}].[concept] c
                ON c.vocabulary_id = 'LOINC'
               AND c.concept_code = lc.loinc
            ),
            source_summary AS (
              SELECT
                loinc,
                COUNT(concept_id) AS source_candidate_count,
                CASE WHEN COUNT(concept_id) = 1 THEN MAX(concept_id) END AS source_concept_id,
                CASE WHEN COUNT(concept_id) = 1 THEN MAX(domain_id) END AS source_domain,
                CASE WHEN COUNT(concept_id) = 1 THEN MAX(standard_concept) END AS source_standard_concept,
                CASE WHEN COUNT(concept_id) = 1 THEN MAX(invalid_reason) END AS source_invalid_reason
              FROM source_candidates
              GROUP BY loinc
            ),
            mapped_targets AS (
              SELECT DISTINCT
                ss.loinc,
                tgt.concept_id AS target_concept_id
              FROM source_summary ss
              JOIN [{target_schema}].[concept_relationship] cr
                ON cr.concept_id_1 = ss.source_concept_id
               AND cr.relationship_id = 'Maps to'
               AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
              JOIN [{target_schema}].[concept] tgt
                ON tgt.concept_id = cr.concept_id_2
               AND tgt.standard_concept = 'S'
               AND tgt.invalid_reason IS NULL
               AND tgt.domain_id = 'Measurement'
              WHERE ss.source_candidate_count = 1
                AND NOT (
                  ss.source_invalid_reason IS NULL
                  AND COALESCE(ss.source_standard_concept, '') = 'S'
                )
            ),
            mapped_counts AS (
              SELECT loinc, COUNT(DISTINCT target_concept_id) AS target_count
              FROM mapped_targets
              GROUP BY loinc
            ),
            code_policy AS (
              SELECT
                ss.loinc,
                ss.source_candidate_count,
                COALESCE(mc.target_count, 0) AS target_count,
                CASE
                  WHEN ss.source_candidate_count > 1
                    THEN 'multiple_source_concepts_to_measurement_zero'
                  WHEN ss.source_candidate_count = 0
                    THEN 'source_concept_not_found_to_measurement_zero'
                  WHEN ss.source_invalid_reason IS NULL
                   AND ss.source_standard_concept = 'S'
                   AND ss.source_domain = 'Measurement'
                    THEN 'direct_standard_measurement'
                  WHEN ss.source_invalid_reason IS NULL
                   AND ss.source_standard_concept = 'S'
                   AND ss.source_domain <> 'Measurement'
                    THEN 'standard_other_domain'
                  WHEN COALESCE(mc.target_count, 0) = 1
                    THEN 'unique_maps_to_standard_measurement'
                  WHEN COALESCE(mc.target_count, 0) > 1
                    THEN 'multiple_measurement_targets_to_measurement_zero'
                  ELSE 'no_measurement_target_to_measurement_zero'
                END AS mapping_class
              FROM source_summary ss
              LEFT JOIN mapped_counts mc
                ON (mc.loinc = ss.loinc OR (mc.loinc IS NULL AND ss.loinc IS NULL))
            ),
            event_policy AS (
              SELECT cp.mapping_class
              FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
              JOIN code_policy cp
                ON (
                     cp.loinc = NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC))), '')
                     OR (
                       cp.loinc IS NULL
                       AND NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC))), '') IS NULL
                     )
                   )
            )
            """

            class_rows = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    text(
                        cte
                        + """
                        SELECT mapping_class, COUNT_BIG(*) AS n
                        FROM event_policy
                        GROUP BY mapping_class
                        ORDER BY mapping_class
                        """
                    )
                ).fetchall()
            }

            covered_rows = sum(class_rows.values())
            if covered_rows != source_rows:
                raise RuntimeError(
                    "LAB mapping audit did not classify every source row: "
                    f"source={source_rows:,}, classified={covered_rows:,}"
                )

            source_multiplicity = {
                str(row[0]): {"code_keys": int(row[1]), "event_rows": int(row[2])}
                for row in connection.execute(
                    text(
                        cte
                        + f"""
                        SELECT
                          CASE
                            WHEN cp.source_candidate_count = 0 THEN 'no_candidate'
                            WHEN cp.source_candidate_count = 1 THEN 'unique_candidate'
                            ELSE 'multiple_candidates'
                          END AS candidate_class,
                          COUNT(DISTINCT COALESCE(cp.loinc, '<NULL_OR_EMPTY>')) AS code_keys,
                          COUNT_BIG(*) AS event_rows
                        FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                        JOIN code_policy cp
                          ON (
                               cp.loinc = NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC))), '')
                               OR (
                                 cp.loinc IS NULL
                                 AND NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC))), '') IS NULL
                               )
                             )
                        GROUP BY
                          CASE
                            WHEN cp.source_candidate_count = 0 THEN 'no_candidate'
                            WHEN cp.source_candidate_count = 1 THEN 'unique_candidate'
                            ELSE 'multiple_candidates'
                          END
                        """
                    )
                ).fetchall()
            }

            ambiguous_target_rows = (
                class_rows.get("multiple_measurement_targets_to_measurement_zero", 0)
            )
            ambiguous_source_rows = (
                class_rows.get("multiple_source_concepts_to_measurement_zero", 0)
            )
            unresolved_measurement_zero_rows = sum(
                class_rows.get(k, 0)
                for k in (
                    "multiple_source_concepts_to_measurement_zero",
                    "source_concept_not_found_to_measurement_zero",
                    "multiple_measurement_targets_to_measurement_zero",
                    "no_measurement_target_to_measurement_zero",
                )
            )
            standard_other_domain_rows = class_rows.get("standard_other_domain", 0)
            expected_measurement_rows = source_rows - standard_other_domain_rows

        payload = {
            "stage": "measurement_lab_mapping_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_rows": source_rows,
            "classified_rows": covered_rows,
            "mapping_classes": class_rows,
            "source_concept_multiplicity": source_multiplicity,
            "ambiguous_source_rows": ambiguous_source_rows,
            "ambiguous_measurement_target_rows": ambiguous_target_rows,
            "unresolved_measurement_concept_zero_rows": unresolved_measurement_zero_rows,
            "standard_other_domain_rows": standard_other_domain_rows,
            "expected_measurement_rows_under_policy": expected_measurement_rows,
            "policy": (
                "Use only a unique exact LOINC source concept. Active Standard Measurement concepts map directly. "
                "Nonstandard or invalid concepts map only to a unique active Standard Measurement target. "
                "Active Standard concepts in another domain route outside Measurement. All other LAB events are "
                "retained as Measurement with measurement_concept_id=0 and source lineage preserved."
            ),
            "status": "matched",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    from .config import load_etl_config

    result = audit_measurement_lab_mapping(load_etl_config(args.config))
    print(f"Source rows: {result['source_rows']:,}")
    for key, value in result["mapping_classes"].items():
        print(f"{key}: {value:,}")
    print(f"Ambiguous source rows: {result['ambiguous_source_rows']:,}")
    print(
        "Ambiguous Measurement target rows: "
        f"{result['ambiguous_measurement_target_rows']:,}"
    )
    print(
        "Unresolved retained as Measurement concept 0: "
        f"{result['unresolved_measurement_concept_zero_rows']:,}"
    )
    print(
        "Expected LAB Measurement rows under policy: "
        f"{result['expected_measurement_rows_under_policy']:,}"
    )
    print(f"Audit: {result['audit_path']}")


if __name__ == "__main__":
    main()
