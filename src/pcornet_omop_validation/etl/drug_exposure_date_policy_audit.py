from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


TYPE_CONCEPTS = {
    "PRESCRIBING": 32838,
    "DISPENSING": 32825,
    "MED_ADMIN": 32818,
    "IMMUNIZATION": 32818,
    "PROCEDURES": 38000179,
}


def _schema(value: object, label: str) -> str:
    out = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", out) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {out!r}")
    return out


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one())


def audit_drug_exposure_date_policy(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for table in (
                "PCORnet_PRESCRIBING",
                "PCORnet_DISPENSING",
                "PCORnet_MED_ADMIN",
                "PCORnet_IMMUNIZATION",
                "PCORnet_PROCEDURES",
            ):
                if not table_exists(con, source_schema, table):
                    raise RuntimeError(f"Missing source table {source_schema}.{table}")
            for table in (
                "etl_drug_event_route",
                "etl_procedure_event_route",
                "person",
                "concept",
            ):
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(f"Missing target table {target_schema}.{table}")

            metrics = {
                "prescribing_source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_PRESCRIBING')}"),
                "prescribing_missing_start_basis": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM {s('PCORnet_PRESCRIBING')}
                    WHERE RX_START_DATE IS NULL AND RX_ORDER_DATE IS NULL
                    """,
                ),
                "prescribing_reversed_source_interval": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM {s('PCORnet_PRESCRIBING')}
                    WHERE RX_END_DATE IS NOT NULL
                      AND COALESCE(CAST(RX_START_DATE AS date), CAST(RX_ORDER_DATE AS date)) IS NOT NULL
                      AND CAST(RX_END_DATE AS date) < COALESCE(CAST(RX_START_DATE AS date), CAST(RX_ORDER_DATE AS date))
                    """,
                ),
                "dispensing_source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_DISPENSING')}"),
                "dispensing_missing_start_date": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM {s('PCORnet_DISPENSING')} WHERE DISPENSE_DATE IS NULL",
                ),
                "med_admin_source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_MED_ADMIN')}"),
                "med_admin_missing_start_date": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM {s('PCORnet_MED_ADMIN')} WHERE MEDADMIN_START_DATE IS NULL",
                ),
                "med_admin_missing_stop_date_with_start": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM {s('PCORnet_MED_ADMIN')}
                    WHERE MEDADMIN_START_DATE IS NOT NULL AND MEDADMIN_STOP_DATE IS NULL
                    """,
                ),
                "med_admin_reversed_interval": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM {s('PCORnet_MED_ADMIN')}
                    WHERE MEDADMIN_START_DATE IS NOT NULL
                      AND MEDADMIN_STOP_DATE IS NOT NULL
                      AND CAST(MEDADMIN_STOP_DATE AS date) < CAST(MEDADMIN_START_DATE AS date)
                    """,
                ),
                "immunization_source_rows": _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_IMMUNIZATION')}"),
                "immunization_missing_admin_date": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM {s('PCORnet_IMMUNIZATION')} WHERE VX_ADMIN_DATE IS NULL",
                ),
                "procedure_drug_route_rows": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM {t('etl_drug_event_route')} WHERE source_domain = 'PROCEDURES'",
                ),
                "procedure_drug_missing_px_date": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {t('etl_drug_event_route')} r
                    LEFT JOIN {s('PCORnet_PROCEDURES')} p
                      ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PROCEDURESID))) = r.source_record_id
                    WHERE r.source_domain = 'PROCEDURES'
                      AND p.PX_DATE IS NULL
                    """,
                ),
            }

            type_rows: dict[str, dict[str, object]] = {}
            for family, cid in TYPE_CONCEPTS.items():
                row = con.execute(
                    text(f"""
                        SELECT concept_id, domain_id, vocabulary_id,
                               concept_code, standard_concept, invalid_reason
                        FROM {t('concept')}
                        WHERE concept_id = :cid
                    """),
                    {"cid": cid},
                ).mappings().first()
                type_rows[family] = dict(row) if row is not None else {"concept_id": cid, "missing": True}

            type_semantics_valid = all(
                not v.get("missing")
                and v.get("domain_id") in ("Type Concept", "Drug Type")
                and v.get("invalid_reason") is None
                for v in type_rows.values()
            )

            checks = {
                "prescribing_required_start_complete": metrics["prescribing_missing_start_basis"] == 0,
                "dispensing_required_start_complete": metrics["dispensing_missing_start_date"] == 0,
                "med_admin_required_start_complete": metrics["med_admin_missing_start_date"] == 0,
                "immunization_required_start_complete": metrics["immunization_missing_admin_date"] == 0,
                "procedure_drug_required_date_complete": metrics["procedure_drug_missing_px_date"] == 0,
                "drug_type_concepts_valid": type_semantics_valid,
            }

            payload = {
                "stage": "drug_exposure_date_policy_audit",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_schema": source_schema,
                "target_schema": target_schema,
                **metrics,
                "type_concepts": type_rows,
                "checks": checks,
                "status": "matched" if all(checks.values()) else "review_required",
                "policy": {
                    "missing_required_start_date": "exclude and quantify; never invent a sentinel date",
                    "prescribing_end": "valid RX_END_DATE; otherwise reversed source end clamps to start; otherwise positive days supply derives end; otherwise start",
                    "dispensing_end": "positive days supply derives end; otherwise same-day exposure",
                    "med_admin_end": "valid stop date when on/after start; otherwise same-day exposure with source stop retained separately when available",
                    "immunization_end": "same-day exposure",
                    "procedure_end": "same-day exposure using PX_DATE",
                },
            }
    finally:
        engine.dispose()

    audit_path = config.audit_dir / "drug_exposure_date_policy_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    payload["audit_path"] = str(audit_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_etl_config(args.config)
    out = audit_drug_exposure_date_policy(cfg)
    print(f"status: {out['status']}")
    for key in (
        "prescribing_source_rows",
        "prescribing_missing_start_basis",
        "prescribing_reversed_source_interval",
        "dispensing_source_rows",
        "dispensing_missing_start_date",
        "med_admin_source_rows",
        "med_admin_missing_start_date",
        "med_admin_missing_stop_date_with_start",
        "med_admin_reversed_interval",
        "immunization_source_rows",
        "immunization_missing_admin_date",
        "procedure_drug_route_rows",
        "procedure_drug_missing_px_date",
    ):
        print(f"{key}: {out[key]}")
    print("type concepts:")
    for family, row in out["type_concepts"].items():
        print(f"  {family}: {row}")
    print("checks:")
    for key, value in out["checks"].items():
        print(f"  {key}: {value}")
    print(f"Audit: {out['audit_path']}")


if __name__ == "__main__":
    main()
