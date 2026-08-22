from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


EXCLUDED_CODES = ("NI", "UN", "OT")
XWALK_TABLE = "etl_drug_exposure_xwalk"


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def finalize_drug_routes(config: EtlConfig) -> dict[str, object]:
    """Finalize route_concept_id from standardized PCORnet route semantics.

    The raw route text remains in route_source_value.  Mapping uses only the
    standardized PCORnet route fields recovered through the Drug lineage table.
    A standardized route code maps only when code/name matching yields exactly
    one active Standard OMOP Route concept.  NI, UN, OT, missing, ambiguous, and
    otherwise unresolved codes remain route_concept_id=0.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "drug_route_finalize.json"

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"
    engine = make_engine(config)

    try:
        with engine.begin() as con:
            source_required = (
                "PCORnet_PRESCRIBING",
                "PCORnet_DISPENSING",
                "PCORnet_MED_ADMIN",
                "PCORnet_IMMUNIZATION",
            )
            target_required = (
                "drug_exposure",
                XWALK_TABLE,
                "concept",
            )
            for table in source_required:
                if not table_exists(con, source_schema, table):
                    raise RuntimeError(
                        f"Required table [{source_schema}].[{table}] does not exist"
                    )
            for table in target_required:
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(
                        f"Required table [{target_schema}].[{table}] does not exist"
                    )

            total_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')}")
            lineage_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}")
            if total_rows != lineage_rows:
                raise RuntimeError(
                    "Drug route finalization requires one lineage row per Drug Exposure: "
                    f"drug_exposure={total_rows:,}, lineage={lineage_rows:,}"
                )

            con.execute(
                text("IF OBJECT_ID('tempdb..#route_source') IS NOT NULL DROP TABLE #route_source")
            )
            con.execute(
                text("IF OBJECT_ID('tempdb..#route_candidates') IS NOT NULL DROP TABLE #route_candidates")
            )
            con.execute(
                text("IF OBJECT_ID('tempdb..#route_map') IS NOT NULL DROP TABLE #route_map")
            )

            # Recover only standardized route fields.  route_source_value is
            # intentionally not used because it can contain RAW_* free text.
            con.execute(
                text(
                    f"""
                    SELECT
                        x.drug_exposure_id,
                        x.source_domain,
                        NULLIF(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), p.RX_ROUTE)))), '') AS route_code
                    INTO #route_source
                    FROM {t(XWALK_TABLE)} x
                    JOIN {s('PCORnet_PRESCRIBING')} p
                      ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PRESCRIBINGID))) = x.source_record_id
                    WHERE x.source_domain = 'PRESCRIBING'

                    UNION ALL

                    SELECT
                        x.drug_exposure_id,
                        x.source_domain,
                        NULLIF(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), d.DISPENSE_ROUTE)))), '')
                    FROM {t(XWALK_TABLE)} x
                    JOIN {s('PCORnet_DISPENSING')} d
                      ON LTRIM(RTRIM(CONVERT(nvarchar(255), d.DISPENSINGID))) = x.source_record_id
                    WHERE x.source_domain = 'DISPENSING'

                    UNION ALL

                    SELECT
                        x.drug_exposure_id,
                        x.source_domain,
                        NULLIF(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), m.MEDADMIN_ROUTE)))), '')
                    FROM {t(XWALK_TABLE)} x
                    JOIN {s('PCORnet_MED_ADMIN')} m
                      ON LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMINID))) = x.source_record_id
                    WHERE x.source_domain = 'MED_ADMIN'

                    UNION ALL

                    SELECT
                        x.drug_exposure_id,
                        x.source_domain,
                        NULLIF(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), i.VX_ROUTE)))), '')
                    FROM {t(XWALK_TABLE)} x
                    JOIN {s('PCORnet_IMMUNIZATION')} i
                      ON LTRIM(RTRIM(CONVERT(nvarchar(255), i.IMMUNIZATIONID))) = x.source_record_id
                    WHERE x.source_domain = 'IMMUNIZATION'
                    """
                )
            )

            route_source_rows = _scalar(con, "SELECT COUNT_BIG(*) FROM #route_source")
            standardized_route_rows = _scalar(
                con,
                "SELECT COUNT_BIG(*) FROM #route_source WHERE route_code IS NOT NULL",
            )
            excluded_rows = _scalar(
                con,
                "SELECT COUNT_BIG(*) FROM #route_source WHERE route_code IN ('NI','UN','OT')",
            )

            con.execute(
                text(
                    f"""
                    WITH codes AS (
                        SELECT DISTINCT route_code
                        FROM #route_source
                        WHERE route_code IS NOT NULL
                          AND route_code NOT IN ('NI','UN','OT')
                    )
                    SELECT DISTINCT
                        c.route_code,
                        v.concept_id
                    INTO #route_candidates
                    FROM codes c
                    JOIN {t('concept')} v
                      ON v.domain_id = 'Route'
                     AND v.standard_concept = 'S'
                     AND v.invalid_reason IS NULL
                     AND (
                          UPPER(LTRIM(RTRIM(v.concept_code))) = c.route_code
                       OR UPPER(LTRIM(RTRIM(v.concept_name))) = c.route_code
                     )
                    """
                )
            )

            con.execute(
                text(
                    """
                    SELECT route_code, MIN(concept_id) AS concept_id
                    INTO #route_map
                    FROM #route_candidates
                    GROUP BY route_code
                    HAVING COUNT(DISTINCT concept_id) = 1
                    """
                )
            )

            mapped_codes = _scalar(con, "SELECT COUNT_BIG(*) FROM #route_map")
            ambiguous_codes = _scalar(
                con,
                """
                SELECT COUNT_BIG(*)
                FROM (
                    SELECT route_code
                    FROM #route_candidates
                    GROUP BY route_code
                    HAVING COUNT(DISTINCT concept_id) > 1
                ) q
                """,
            )

            # A previously finalized nonzero value must agree with the same
            # general rule.  We do not overwrite discrepant nonzero values.
            conflicting_nonzero = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN {t('drug_exposure')} d
                  ON d.drug_exposure_id = r.drug_exposure_id
                LEFT JOIN #route_map m
                  ON m.route_code = r.route_code
                WHERE d.route_concept_id <> 0
                  AND (
                       m.concept_id IS NULL
                    OR d.route_concept_id <> m.concept_id
                  )
                """,
            )
            if conflicting_nonzero:
                raise RuntimeError(
                    "Existing nonzero Drug route concepts conflict with the "
                    f"generalized unique-route rule: {conflicting_nonzero:,} rows"
                )

            rows_eligible_to_map = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN #route_map m ON m.route_code = r.route_code
                JOIN {t('drug_exposure')} d
                  ON d.drug_exposure_id = r.drug_exposure_id
                WHERE d.route_concept_id = 0
                """,
            )

            con.execute(
                text(
                    f"""
                    UPDATE d
                       SET route_concept_id = m.concept_id
                    FROM {t('drug_exposure')} d
                    JOIN #route_source r
                      ON r.drug_exposure_id = d.drug_exposure_id
                    JOIN #route_map m
                      ON m.route_code = r.route_code
                    WHERE d.route_concept_id = 0
                    """
                )
            )

            mapping_mismatch_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN #route_map m ON m.route_code = r.route_code
                JOIN {t('drug_exposure')} d
                  ON d.drug_exposure_id = r.drug_exposure_id
                WHERE d.route_concept_id <> m.concept_id
                """,
            )
            if mapping_mismatch_rows:
                raise RuntimeError(
                    "Drug route update reconciliation failed: "
                    f"{mapping_mismatch_rows:,} uniquely mapped rows disagree"
                )

            invalid_nonzero = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('drug_exposure')} d
                LEFT JOIN {t('concept')} c
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

            mapped_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN #route_map m ON m.route_code = r.route_code
                JOIN {t('drug_exposure')} d
                  ON d.drug_exposure_id = r.drug_exposure_id
                WHERE d.route_concept_id = m.concept_id
                """,
            )
            remaining_standardized_zero = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM #route_source r
                JOIN {t('drug_exposure')} d
                  ON d.drug_exposure_id = r.drug_exposure_id
                WHERE r.route_code IS NOT NULL
                  AND d.route_concept_id = 0
                """,
            )
            all_zero_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')} WHERE route_concept_id = 0",
            )

            mapping_rows = [
                {
                    "route_code": str(row[0]),
                    "concept_id": int(row[1]),
                    "concept_name": str(row[2]),
                    "rows": int(row[3]),
                }
                for row in con.execute(
                    text(
                        f"""
                        SELECT m.route_code, m.concept_id, c.concept_name, COUNT_BIG(*)
                        FROM #route_source r
                        JOIN #route_map m ON m.route_code = r.route_code
                        JOIN {t('concept')} c ON c.concept_id = m.concept_id
                        JOIN {t('drug_exposure')} d
                          ON d.drug_exposure_id = r.drug_exposure_id
                        WHERE d.route_concept_id = m.concept_id
                        GROUP BY m.route_code, m.concept_id, c.concept_name
                        ORDER BY COUNT_BIG(*) DESC, m.route_code
                        """
                    )
                ).all()
            ]

        payload = {
            "stage": "drug_route_finalize",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "drug_exposure_rows": total_rows,
            "lineage_rows": lineage_rows,
            "route_source_rows": route_source_rows,
            "standardized_route_rows": standardized_route_rows,
            "excluded_ni_un_ot_rows": excluded_rows,
            "unique_mapped_codes": mapped_codes,
            "ambiguous_codes": ambiguous_codes,
            "rows_newly_eligible_to_map": rows_eligible_to_map,
            "mapped_rows": mapped_rows,
            "remaining_standardized_zero_rows": remaining_standardized_zero,
            "all_route_concept_zero_rows": all_zero_rows,
            "mapping_rows": mapping_rows,
            "status": "matched",
            "policy": (
                "Exact normalized standardized PCORnet route code/name maps only when "
                "there is exactly one active Standard OMOP Route concept. NI, UN, OT, "
                "missing, ambiguous, and unresolved codes remain concept 0."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
