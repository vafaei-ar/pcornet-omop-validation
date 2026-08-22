from __future__ import annotations

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


def audit_measurement_unit_standard(config: EtlConfig) -> dict[str, object]:
    """Audit LAB unit mapping using case-sensitive UCUM semantics.

    UCUM codes are case-sensitive. This audit compares exact binary matches with
    case-insensitive matches so SQL Server collation cannot silently equate
    semantically distinct unit codes such as ``U`` and ``u``.
    """
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for table in ("PCORnet_LAB_RESULT_CM", "measurement", "concept"):
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(f"Required table dbo.{table} does not exist")

            source_rows = int(
                con.execute(text("SELECT COUNT_BIG(*) FROM dbo.PCORnet_LAB_RESULT_CM")).scalar_one()
            )

            rows = con.execute(
                text(
                    """
                    WITH source_units AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(nvarchar(100), RESULT_UNIT))) AS unit_code,
                            COUNT_BIG(*) AS source_rows
                        FROM dbo.PCORnet_LAB_RESULT_CM
                        WHERE RESULT_UNIT IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(100), RESULT_UNIT))) <> ''
                        GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(100), RESULT_UNIT)))
                    ),
                    exact_candidates AS (
                        SELECT
                            s.unit_code,
                            COUNT(DISTINCT c.concept_id) AS n_exact,
                            MIN(c.concept_id) AS exact_concept_id
                        FROM source_units s
                        LEFT JOIN dbo.concept c
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
                        LEFT JOIN dbo.concept c
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
                    JOIN exact_candidates e ON e.unit_code = s.unit_code
                    JOIN ci_candidates ci ON ci.unit_code = s.unit_code
                    WHERE
                        (e.n_exact = 0 AND ci.n_ci > 0)
                        OR e.n_exact <> ci.n_ci
                        OR ISNULL(e.exact_concept_id, -1) <> ISNULL(ci.ci_concept_id, -1)
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
                    "case_insensitive_concept_id": None if r[5] is None else int(r[5]),
                }
                for r in rows
            ]

            suspicious_current = con.execute(
                text(
                    """
                    SELECT TOP (30)
                        unit_source_value,
                        unit_concept_id,
                        c.concept_code,
                        c.concept_name,
                        COUNT_BIG(*) AS n
                    FROM dbo.measurement m
                    LEFT JOIN dbo.concept c
                      ON c.concept_id = m.unit_concept_id
                    WHERE m.unit_source_value IS NOT NULL
                      AND m.unit_concept_id <> 0
                      AND c.vocabulary_id = 'UCUM'
                      AND c.concept_code COLLATE Latin1_General_100_BIN2 <>
                          m.unit_source_value COLLATE Latin1_General_100_BIN2
                    GROUP BY unit_source_value, unit_concept_id, c.concept_code, c.concept_name
                    ORDER BY COUNT_BIG(*) DESC
                    """
                )
            ).fetchall()

            mismatches = [
                {
                    "unit_source_value": str(r[0]),
                    "unit_concept_id": int(r[1]),
                    "concept_code": str(r[2]),
                    "concept_name": str(r[3]),
                    "rows": int(r[4]),
                }
                for r in suspicious_current
            ]

            return {
                "lab_source_rows": source_rows,
                "case_sensitive_collision_codes": collisions,
                "current_nonexact_ucum_mappings": mismatches,
                "status": "review_required" if collisions or mismatches else "matched",
                "policy": "UCUM concept-code matching must be case-sensitive; no synonym guessing.",
            }
    finally:
        engine.dispose()
