from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


EXCLUDED_CODES = ("NI", "UN", "OT")
EXPECTED_TOTAL = 48_457_880


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def finalize_drug_routes(config: EtlConfig) -> dict[str, object]:
    """Populate route_concept_id from standardized PCORnet route fields.

    route_source_value intentionally preserves RAW_* route text when available.
    Therefore it must not be used as the semantic mapping key.  Mapping uses the
    standardized PCORnet route fields recovered through the drug lineage table.
    """
    audit_path = config.audit_dir / "drug_route_finalize.json"
    engine = make_engine(config)

    try:
        with engine.begin() as con:
            required = (
                "drug_exposure",
                "etl_drug_exposure_xwalk",
                "PCORnet_PRESCRIBING",
                "PCORnet_DISPENSING",
                "PCORnet_MED_ADMIN",
                "PCORnet_IMMUNIZATION",
                "concept",
            )
            for table in required:
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(f"Required table dbo.{table} does not exist")

            total_rows = _scalar(con, "SELECT COUNT_BIG(*) FROM dbo.drug_exposure")
            if total_rows != EXPECTED_TOTAL:
                raise RuntimeError(
                    f"Unexpected drug_exposure row count: {total_rows:,}"
                )

            con.execute(text("IF OBJECT_ID('tempdb..#route_source') IS NOT NULL DROP TABLE #route_source;"))
            con.execute(text("IF OBJECT_ID('tempdb..#route_map') IS NOT NULL DROP TABLE #route_map;"))

            # Recover the standardized PCORnet route code by source family.  The
            # OMOP route_source_value field keeps RAW_* text when available, so
            # using it for concept mapping would incorrectly treat free text as
            # the standardized code.
            con.execute(
                text("""
                SELECT
                    x.drug_exposure_id,
                    x.source_domain,
                    UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), p.RX_ROUTE)))) AS route_code
                INTO #route_source
                FROM dbo.etl_drug_exposure_xwalk x
                JOIN dbo.PCORnet_PRESCRIBING p
                  ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PRESCRIBINGID))) = x.source_record_id
                WHERE x.source_domain = 'PRESCRIBING'

                UNION ALL

                SELECT
                    x.drug_exposure_id,
                    x.source_domain,
                    UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), d.DISPENSE_ROUTE))))
                FROM dbo.etl_drug_exposure_xwalk x
                JOIN dbo.PCORnet_DISPENSING d
                  ON LTRIM(RTRIM(CONVERT(nvarchar(255), d.DISPENSINGID))) = x.source_record_id
                WHERE x.source_domain = 'DISPENSING'

                UNION ALL

                SELECT
                    x.drug_exposure_id,
                    x.source_domain,
                    UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), m.MEDADMIN_ROUTE))))
                FROM dbo.etl_drug_exposure_xwalk x
                JOIN dbo.PCORnet_MED_ADMIN m
                  ON LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMINID))) = x.source_record_id
                WHERE x.source_domain = 'MED_ADMIN'

                UNION ALL

                SELECT
                    x.drug_exposure_id,
                    x.source_domain,
                    UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), i.VX_ROUTE))))
                FROM dbo.etl_drug_exposure_xwalk x
                JOIN dbo.PCORnet_IMMUNIZATION i
                  ON LTRIM(RTRIM(CONVERT(nvarchar(255), i.IMMUNIZATIONID))) = x.source_record_id
                WHERE x.source_domain = 'IMMUNIZATION';
                """)
            )

            standardized_route_rows = _scalar(
                con,
                """
                SELECT COUNT_BIG(*) FROM #route_source
                WHERE route_code IS NOT NULL AND route_code <> ''
                """,
            )
            excluded_rows = _scalar(
                con,
                """
                SELECT COUNT_BIG(*) FROM #route_source
                WHERE route_code IN ('NI','UN','OT')
                """,
            )

            con.execute(
                text("""
                WITH codes AS (
                    SELECT DISTINCT route_code
                    FROM #route_source
                    WHERE route_code IS NOT NULL
                      AND route_code <> ''
                      AND route_code NOT IN ('NI','UN','OT')
                ),
                candidates AS (
                    SELECT DISTINCT c.route_code, v.concept_id
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
                    SELECT route_code, MIN(concept_id) AS concept_id
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
                    SELECT DISTINCT route_code
                    FROM #route_source
                    WHERE route_code IS NOT NULL
                      AND route_code <> ''
                      AND route_code NOT IN ('NI','UN','OT')
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
                SELECT COUNT_BIG(*) FROM candidate_counts WHERE n > 1
                """,
            )

            rows_eligible_to_map = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN #route_map m ON m.route_code = r.route_code
                JOIN dbo.drug_exposure d ON d.drug_exposure_id = r.drug_exposure_id
                WHERE d.route_concept_id = 0
                """,
            )

            con.execute(
                text("""
                UPDATE d
                   SET route_concept_id = m.concept_id
                FROM dbo.drug_exposure d
                JOIN #route_source r
                  ON r.drug_exposure_id = d.drug_exposure_id
                JOIN #route_map m
                  ON m.route_code = r.route_code
                WHERE d.route_concept_id = 0;
                """)
            )

            mapped_nonzero_rows = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN #route_map m ON m.route_code = r.route_code
                JOIN dbo.drug_exposure d ON d.drug_exposure_id = r.drug_exposure_id
                WHERE d.route_concept_id = m.concept_id
                """,
            )

            if mapped_nonzero_rows < rows_eligible_to_map:
                raise RuntimeError(
                    "Drug route update reconciliation failed: "
                    f"eligible={rows_eligible_to_map:,}, now_mapped={mapped_nonzero_rows:,}"
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

            remaining_standardized_zero = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN dbo.drug_exposure d ON d.drug_exposure_id = r.drug_exposure_id
                WHERE r.route_code IS NOT NULL
                  AND r.route_code <> ''
                  AND d.route_concept_id = 0
                """,
            )

            mapping_rows = [
                {
                    "route_code": str(r[0]),
                    "concept_id": int(r[1]),
                    "concept_name": str(r[2]),
                    "rows": int(r[3]),
                }
                for r in con.execute(
                    text("""
                    SELECT
                        m.route_code,
                        m.concept_id,
                        c.concept_name,
                        COUNT_BIG(*) AS n
                    FROM #route_map m
                    JOIN dbo.concept c ON c.concept_id = m.concept_id
                    JOIN #route_source r ON r.route_code = m.route_code
                    GROUP BY m.route_code, m.concept_id, c.concept_name
                    ORDER BY COUNT_BIG(*) DESC, m.route_code
                    """)
                ).fetchall()
            ]

        payload = {
            "stage": "drug_route_finalize",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "policy": (
                "Preserve RAW route text in route_source_value, but map route_concept_id "
                "from the standardized PCORnet route field. Map only exact case-normalized "
                "codes with exactly one active Standard Route-domain concept; keep NI/UN/OT, "
                "ambiguous codes, and unmatched values at concept_id 0."
            ),
            "excluded_codes": list(EXCLUDED_CODES),
            "standardized_route_rows": standardized_route_rows,
            "excluded_standardized_rows": excluded_rows,
            "mapped_codes": mapped_codes,
            "ambiguous_codes": ambiguous_codes,
            "mapped_rows_this_run": rows_eligible_to_map,
            "mapped_rows_total": mapped_nonzero_rows,
            "remaining_standardized_route_zero_rows": remaining_standardized_zero,
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
