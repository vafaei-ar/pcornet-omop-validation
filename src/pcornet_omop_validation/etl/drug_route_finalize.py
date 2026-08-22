from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


EXCLUDED_CODES = ("NI", "UN", "OT")


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def finalize_drug_routes(config: EtlConfig) -> dict[str, object]:
    audit_path = config.audit_dir / "drug_route_finalize.json"
    engine = make_engine(config)

    try:
        with engine.begin() as con:
            for table in ("drug_exposure", "concept"):
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(f"Required table dbo.{table} does not exist")

            total_rows = _scalar(con, "SELECT COUNT_BIG(*) FROM dbo.drug_exposure")
            if total_rows != 48_457_880:
                raise RuntimeError(
                    f"Unexpected drug_exposure row count: {total_rows:,}"
                )

            before_zero_nonblank = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM dbo.drug_exposure
                WHERE route_concept_id = 0
                  AND route_source_value IS NOT NULL
                  AND LTRIM(RTRIM(route_source_value)) <> ''
                """,
            )

            con.execute(text("IF OBJECT_ID('tempdb..#route_map') IS NOT NULL DROP TABLE #route_map;"))
            con.execute(
                text("""
                WITH codes AS (
                    SELECT DISTINCT
                        UPPER(LTRIM(RTRIM(route_source_value))) AS route_code
                    FROM dbo.drug_exposure
                    WHERE route_source_value IS NOT NULL
                      AND LTRIM(RTRIM(route_source_value)) <> ''
                      AND UPPER(LTRIM(RTRIM(route_source_value))) NOT IN ('NI','UN','OT')
                ),
                candidates AS (
                    SELECT DISTINCT
                        c.route_code,
                        v.concept_id
                    FROM codes c
                    JOIN dbo.concept v
                      ON v.domain_id = 'Route'
                     AND v.standard_concept = 'S'
                     AND v.invalid_reason IS NULL
                     AND (
                          UPPER(v.concept_code) = c.route_code
                       OR UPPER(v.concept_name) = c.route_code
                     )
                ),
                unique_map AS (
                    SELECT
                        route_code,
                        MIN(concept_id) AS concept_id
                    FROM candidates
                    GROUP BY route_code
                    HAVING COUNT(DISTINCT concept_id) = 1
                )
                SELECT route_code, concept_id
                INTO #route_map
                FROM unique_map;
                """)
            )

            mapped_codes = _scalar(con, "SELECT COUNT_BIG(*) FROM #route_map")
            ambiguous_codes = _scalar(
                con,
                """
                WITH codes AS (
                    SELECT DISTINCT UPPER(LTRIM(RTRIM(route_source_value))) AS route_code
                    FROM dbo.drug_exposure
                    WHERE route_source_value IS NOT NULL
                      AND LTRIM(RTRIM(route_source_value)) <> ''
                      AND UPPER(LTRIM(RTRIM(route_source_value))) NOT IN ('NI','UN','OT')
                ),
                candidate_counts AS (
                    SELECT c.route_code, COUNT(DISTINCT v.concept_id) AS n
                    FROM codes c
                    JOIN dbo.concept v
                      ON v.domain_id = 'Route'
                     AND v.standard_concept = 'S'
                     AND v.invalid_reason IS NULL
                     AND (
                          UPPER(v.concept_code) = c.route_code
                       OR UPPER(v.concept_name) = c.route_code
                     )
                    GROUP BY c.route_code
                )
                SELECT COUNT_BIG(*)
                FROM candidate_counts
                WHERE n > 1
                """,
            )

            rows_eligible_to_map = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM dbo.drug_exposure d
                JOIN #route_map m
                  ON m.route_code = UPPER(LTRIM(RTRIM(d.route_source_value)))
                WHERE d.route_concept_id = 0
                """,
            )

            con.execute(
                text("""
                UPDATE d
                   SET route_concept_id = m.concept_id
                FROM dbo.drug_exposure d
                JOIN #route_map m
                  ON m.route_code = UPPER(LTRIM(RTRIM(d.route_source_value)))
                WHERE d.route_concept_id = 0;
                """)
            )

            after_zero_nonblank = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM dbo.drug_exposure
                WHERE route_concept_id = 0
                  AND route_source_value IS NOT NULL
                  AND LTRIM(RTRIM(route_source_value)) <> ''
                """,
            )
            mapped_rows = before_zero_nonblank - after_zero_nonblank

            if mapped_rows != rows_eligible_to_map:
                raise RuntimeError(
                    "Drug route update reconciliation failed: "
                    f"expected {rows_eligible_to_map:,}, mapped {mapped_rows:,}"
                )

            invalid_nonzero = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM dbo.drug_exposure d
                LEFT JOIN dbo.concept c
                  ON c.concept_id = d.route_concept_id
                WHERE d.route_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Route'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_nonzero:
                raise RuntimeError(
                    f"Invalid nonzero route concepts found: {invalid_nonzero:,}"
                )

            mapping_rows = [
                {
                    "route_code": str(r[0]),
                    "concept_id": int(r[1]),
                    "concept_name": str(r[2]),
                }
                for r in con.execute(
                    text("""
                    SELECT m.route_code, m.concept_id, c.concept_name
                    FROM #route_map m
                    JOIN dbo.concept c ON c.concept_id = m.concept_id
                    ORDER BY m.route_code
                    """)
                ).fetchall()
            ]

        payload = {
            "stage": "drug_route_finalize",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "policy": (
                "Map only exact case-normalized route source values with exactly one active "
                "Standard Route-domain concept match by concept name or concept code; keep "
                "NI/UN/OT, ambiguous values, and unmatched free text at concept_id 0."
            ),
            "excluded_codes": list(EXCLUDED_CODES),
            "mapped_codes": mapped_codes,
            "ambiguous_codes": ambiguous_codes,
            "mapped_rows": mapped_rows,
            "nonblank_route_zero_rows_before": before_zero_nonblank,
            "nonblank_route_zero_rows_after": after_zero_nonblank,
            "invalid_nonzero_route_rows": invalid_nonzero,
            "mappings": mapping_rows,
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
