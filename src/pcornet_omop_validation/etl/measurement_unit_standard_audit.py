from __future__ import annotations

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_measurement_xwalk"


def audit_measurement_unit_standard(config: EtlConfig) -> dict[str, object]:
    """Audit LAB-derived Measurement units against exact UCUM semantics.

    UCUM concept-code matching is case-sensitive. Collision diagnostics compare
    exact binary matches with case-insensitive matches, but collisions alone do
    not imply a materialized ETL defect. Status is determined by whether
    LAB-derived Measurement rows agree with the desired exact, case-sensitive,
    unique active Standard UCUM Unit mapping.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (source_schema, XWALK_TABLE),
                (target_schema, "measurement"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            source_rows = int(
                con.execute(
                    text(
                        f"SELECT COUNT_BIG(*) "
                        f"FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]"
                    )
                ).scalar_one()
            )

            collision_rows = con.execute(
                text(
                    f"""
                    WITH source_units AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(
                                nvarchar(100), RESULT_UNIT
                            ))) AS unit_code,
                            COUNT_BIG(*) AS source_rows
                        FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                        WHERE RESULT_UNIT IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(
                                nvarchar(100), RESULT_UNIT
                              ))) <> ''
                        GROUP BY LTRIM(RTRIM(CONVERT(
                            nvarchar(100), RESULT_UNIT
                        )))
                    ),
                    exact_candidates AS (
                        SELECT
                            s.unit_code,
                            COUNT(DISTINCT c.concept_id) AS n_exact,
                            MIN(c.concept_id) AS exact_concept_id
                        FROM source_units s
                        LEFT JOIN [{target_schema}].[concept] c
                          ON c.vocabulary_id = 'UCUM'
                         AND c.domain_id = 'Unit'
                         AND c.standard_concept = 'S'
                         AND c.invalid_reason IS NULL
                         AND c.concept_code COLLATE Latin1_General_100_BIN2 =
                             s.unit_code COLLATE Latin1_General_100_BIN2
                        GROUP BY s.unit_code
                    ),
                    ci_candidates AS (
                        SELECT
                            s.unit_code,
                            COUNT(DISTINCT c.concept_id) AS n_ci,
                            MIN(c.concept_id) AS ci_concept_id
                        FROM source_units s
                        LEFT JOIN [{target_schema}].[concept] c
                          ON c.vocabulary_id = 'UCUM'
                         AND c.domain_id = 'Unit'
                         AND c.standard_concept = 'S'
                         AND c.invalid_reason IS NULL
                         AND UPPER(c.concept_code) = UPPER(s.unit_code)
                        GROUP BY s.unit_code
                    )
                    SELECT TOP (50)
                        s.unit_code,
                        s.source_rows,
                        e.n_exact,
                        e.exact_concept_id,
                        ci.n_ci,
                        ci.ci_concept_id
                    FROM source_units s
                    JOIN exact_candidates e
                      ON e.unit_code = s.unit_code
                    JOIN ci_candidates ci
                      ON ci.unit_code = s.unit_code
                    WHERE
                        (e.n_exact = 0 AND ci.n_ci > 0)
                        OR e.n_exact <> ci.n_ci
                        OR ISNULL(e.exact_concept_id, -1) <>
                           ISNULL(ci.ci_concept_id, -1)
                    ORDER BY s.source_rows DESC, s.unit_code
                    """
                )
            ).fetchall()

            collisions = [
                {
                    "unit_code": str(r[0]),
                    "source_rows": int(r[1]),
                    "exact_candidates": int(r[2]),
                    "exact_concept_id": None if r[3] is None else int(r[3]),
                    "case_insensitive_candidates": int(r[4]),
                    "case_insensitive_concept_id": (
                        None if r[5] is None else int(r[5])
                    ),
                }
                for r in collision_rows
            ]

            mismatch_rows = con.execute(
                text(
                    f"""
                    WITH unit_candidates AS (
                        SELECT
                            c.concept_code COLLATE Latin1_General_100_BIN2
                                AS concept_code,
                            c.concept_id,
                            COUNT_BIG(*) OVER (
                                PARTITION BY
                                    c.concept_code COLLATE Latin1_General_100_BIN2
                            ) AS candidate_count
                        FROM [{target_schema}].[concept] c
                        WHERE c.vocabulary_id = 'UCUM'
                          AND c.domain_id = 'Unit'
                          AND c.standard_concept = 'S'
                          AND c.invalid_reason IS NULL
                    ),
                    unit_map AS (
                        SELECT
                            concept_code,
                            MAX(
                                CASE WHEN candidate_count = 1
                                     THEN concept_id ELSE NULL END
                            ) AS desired_unit_concept_id
                        FROM unit_candidates
                        GROUP BY concept_code
                    )
                    SELECT TOP (50)
                        NULLIF(LTRIM(RTRIM(CONVERT(
                            nvarchar(50), l.RESULT_UNIT
                        ))), '') AS unit_source_value,
                        m.unit_concept_id,
                        m.unit_source_concept_id,
                        COALESCE(um.desired_unit_concept_id, 0)
                            AS desired_unit_concept_id,
                        COUNT_BIG(*) AS n
                    FROM [{target_schema}].[measurement] m
                    JOIN [{source_schema}].[{XWALK_TABLE}] x
                      ON x.measurement_id = m.measurement_id
                     AND x.source_family = 'LAB_RESULT_CM'
                    JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                      ON LTRIM(RTRIM(CONVERT(
                           nvarchar(255), l.LAB_RESULT_CM_ID
                         ))) = x.source_record_id
                    LEFT JOIN unit_map um
                      ON um.concept_code =
                         NULLIF(LTRIM(RTRIM(CONVERT(
                           nvarchar(50), l.RESULT_UNIT
                         ))), '') COLLATE Latin1_General_100_BIN2
                    WHERE m.unit_concept_id <>
                              COALESCE(um.desired_unit_concept_id, 0)
                       OR m.unit_source_concept_id <>
                              COALESCE(um.desired_unit_concept_id, 0)
                    GROUP BY
                        NULLIF(LTRIM(RTRIM(CONVERT(
                            nvarchar(50), l.RESULT_UNIT
                        ))), ''),
                        m.unit_concept_id,
                        m.unit_source_concept_id,
                        COALESCE(um.desired_unit_concept_id, 0)
                    ORDER BY COUNT_BIG(*) DESC, unit_source_value
                    """
                )
            ).fetchall()

            mismatches = [
                {
                    "unit_source_value": None if r[0] is None else str(r[0]),
                    "unit_concept_id": int(r[1]),
                    "unit_source_concept_id": int(r[2]),
                    "desired_unit_concept_id": int(r[3]),
                    "rows": int(r[4]),
                }
                for r in mismatch_rows
            ]

            current_nonexact_mapping_rows = sum(
                int(item["rows"]) for item in mismatches
            )

            return {
                "lab_source_rows": source_rows,
                "case_sensitive_collision_codes": collisions,
                "current_nonexact_ucum_mappings": mismatches,
                "current_nonexact_mapping_rows": current_nonexact_mapping_rows,
                "status": "matched" if not mismatches else "review_required",
                "policy": (
                    "LAB RESULT_UNIT maps only to an exact case-sensitive, "
                    "unique active Standard UCUM Unit concept; otherwise "
                    "concept_id=0 with unit_source_value preserved."
                ),
            }
    finally:
        engine.dispose()
