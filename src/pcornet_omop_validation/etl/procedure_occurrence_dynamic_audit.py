from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_procedure_event_route"
XWALK_TABLE = "etl_procedure_occurrence_xwalk"


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one())


def audit_procedure_occurrence_dynamic(config: EtlConfig) -> dict[str, object]:
    """Non-mutating reconciliation of Procedure-domain route rows to OMOP.

    PCORnet source data live in source_schema. ETL route/lineage tables and OMOP
    tables live in target_schema. Expectations are derived from the route ledger,
    not from site-specific row-count constants.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "procedure_occurrence_dynamic_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_PROCEDURES"),
                (target_schema, ROUTE_TABLE),
                (target_schema, "etl_visit_occurrence_xwalk"),
                (target_schema, "person"),
                (target_schema, "procedure_occurrence"),
                (target_schema, "concept"),
                (target_schema, XWALK_TABLE),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            source_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_PROCEDURES]",
            )
            excluded_missing_px_date = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_PROCEDURES] WHERE PX_DATE IS NULL",
            )
            route_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] WHERE target_domain='Procedure'",
            )
            route_distinct_source_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(DISTINCT source_procedure_id) FROM [{target_schema}].[{ROUTE_TABLE}] WHERE target_domain='Procedure'",
            )
            route_concept_zero_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] WHERE target_domain='Procedure' AND COALESCE(target_concept_id,0)=0",
            )
            invalid_standard_target_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id=r.target_concept_id
                WHERE r.target_domain='Procedure'
                  AND COALESCE(r.target_concept_id,0)<>0
                  AND (c.concept_id IS NULL OR c.standard_concept<>'S'
                       OR c.domain_id<>'Procedure' OR c.invalid_reason IS NOT NULL)
                """,
            )

            target_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[procedure_occurrence]")
            target_concept_zero_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[procedure_occurrence] WHERE procedure_concept_id=0",
            )
            xwalk_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]")

            route_target_mismatch_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[procedure_occurrence] p
                  ON p.procedure_occurrence_id=r.route_id
                LEFT JOIN [{target_schema}].[person] pe
                  ON pe.person_source_value=r.patid
                LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                  ON v.encounterid=r.encounterid
                WHERE r.target_domain='Procedure'
                  AND (
                       p.procedure_occurrence_id IS NULL
                    OR p.person_id<>pe.person_id
                    OR COALESCE(p.procedure_concept_id,0)<>COALESCE(r.target_concept_id,0)
                    OR p.procedure_date<>CAST(r.px_date AS date)
                    OR COALESCE(p.procedure_source_concept_id,0)<>COALESCE(r.source_concept_id,0)
                    OR COALESCE(p.visit_occurrence_id,-1)<>COALESCE(v.visit_occurrence_id,-1)
                  )
                """,
            )

            xwalk_mismatch_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[{XWALK_TABLE}] x
                  ON x.route_id=r.route_id
                WHERE r.target_domain='Procedure'
                  AND (
                       x.route_id IS NULL
                    OR x.procedure_occurrence_id<>r.route_id
                    OR x.source_procedure_id<>r.source_procedure_id
                    OR x.target_ordinal<>r.target_ordinal
                    OR COALESCE(x.source_concept_id,0)<>COALESCE(r.source_concept_id,0)
                    OR COALESCE(x.target_concept_id,0)<>COALESCE(r.target_concept_id,0)
                    OR x.route_status<>r.route_status
                  )
                """,
            )

            checks = {
                "route_vs_target": target_rows == route_rows,
                "route_vs_xwalk": xwalk_rows == route_rows,
                "route_target_match": route_target_mismatch_rows == 0,
                "xwalk_route_match": xwalk_mismatch_rows == 0,
                "concept_zero_match": target_concept_zero_rows == route_concept_zero_rows,
                "standard_procedure_semantics": invalid_standard_target_rows == 0,
            }
            status = "matched" if all(checks.values()) else "review_required"

            payload = {
                "stage": "procedure_occurrence_dynamic_audit",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_schema": source_schema,
                "target_schema": target_schema,
                "source_rows": source_rows,
                "excluded_missing_px_date": excluded_missing_px_date,
                "route_rows": route_rows,
                "route_distinct_source_rows": route_distinct_source_rows,
                "one_to_many_extra_rows": route_rows - route_distinct_source_rows,
                "xwalk_rows": xwalk_rows,
                "target_rows": target_rows,
                "route_target_mismatch_rows": route_target_mismatch_rows,
                "xwalk_mismatch_rows": xwalk_mismatch_rows,
                "route_concept_zero_rows": route_concept_zero_rows,
                "target_concept_zero_rows": target_concept_zero_rows,
                "invalid_standard_target_rows": invalid_standard_target_rows,
                "checks": checks,
                "status": status,
            }
    finally:
        engine.dispose()

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload
