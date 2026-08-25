from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .rebuild_readiness import audit_rebuild_readiness


ID_SPECS = (
    ("PCORnet_DEMOGRAPHIC", "PATID"),
    ("PCORnet_ENCOUNTER", "ENCOUNTERID"),
    ("PCORnet_DIAGNOSIS", "DIAGNOSISID"),
    ("PCORnet_CONDITION", "CONDITIONID"),
    ("PCORnet_PROCEDURES", "PROCEDURESID"),
    ("PCORnet_LAB_RESULT_CM", "LAB_RESULT_CM_ID"),
    ("PCORnet_VITAL", "VITALID"),
    ("PCORnet_OBS_CLIN", "OBSCLINID"),
    ("PCORnet_OBS_GEN", "OBSGENID"),
    ("PCORnet_PRESCRIBING", "PRESCRIBINGID"),
    ("PCORnet_DISPENSING", "DISPENSINGID"),
    ("PCORnet_MED_ADMIN", "MEDADMINID"),
    ("PCORnet_IMMUNIZATION", "IMMUNIZATIONID"),
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _id_profile(con, schema: str, table: str, column: str) -> dict[str, int]:
    missing = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{schema}].[{table}]
        WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), [{column}]))), '') IS NULL
        """,
    )
    duplicate_groups = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM (
          SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), [{column}]))) AS source_id
          FROM [{schema}].[{table}]
          WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), [{column}]))), '') IS NOT NULL
          GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), [{column}])))
          HAVING COUNT_BIG(*) > 1
        ) q
        """,
    )
    return {"missing": missing, "duplicate_groups": duplicate_groups}


def audit_clean_build_preflight(config: EtlConfig) -> dict[str, object]:
    readiness = audit_rebuild_readiness(config)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    policies = config.raw.get("policies", {}) or {}

    blockers: list[str] = []
    warnings: list[str] = []

    if readiness.get("status") != "ready_for_clean_build":
        blockers.append(
            "Rebuild readiness is not ready_for_clean_build; do not start materialization."
        )

    required_policies = {
        "missing_required_date": "exclude",
        "unmapped_standard_concept": "concept_zero",
        "condition_sources": "include_both",
    }
    policy_checks: dict[str, dict[str, object]] = {}
    for key, expected in required_policies.items():
        observed = policies.get(key)
        matched = observed == expected
        policy_checks[key] = {
            "expected": expected,
            "observed": observed,
            "matched": matched,
        }
        if not matched:
            blockers.append(
                f"Policy {key} must be {expected!r} for the validated clean build; observed {observed!r}."
            )

    if source_schema != target_schema:
        blockers.append(
            "The current procedure/OBS_CLIN route builders have not yet been generalized for "
            "different source_schema and target_schema values. Refusing a build until that is fixed."
        )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            id_profiles: dict[str, dict[str, int]] = {}
            for table, column in ID_SPECS:
                if not table_exists(con, source_schema, table):
                    blockers.append(f"Missing required staged source table {source_schema}.{table}")
                    continue
                profile = _id_profile(con, source_schema, table, column)
                id_profiles[f"{table}.{column}"] = profile

            lab_loinc_null = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                WHERE RESULT_DATE IS NOT NULL
                  AND LAB_LOINC IS NULL
                """,
            )
            lab_loinc_blank = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                WHERE RESULT_DATE IS NOT NULL
                  AND LAB_LOINC IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(100), LAB_LOINC))) = ''
                """,
            )

            missing_required_ids = {
                key: value
                for key, value in id_profiles.items()
                if value["missing"] > 0
            }
            duplicate_required_ids = {
                key: value
                for key, value in id_profiles.items()
                if value["duplicate_groups"] > 0
            }

            # Some source families have explicit exclusion policies for missing IDs,
            # but base lineage generators rely on stable identifiers. Surface these
            # before materialization rather than discovering them after a partial run.
            if duplicate_required_ids:
                blockers.append(
                    "Duplicate staged source identifier groups were detected; route-aware lineage "
                    "requires unique source identifiers for the affected families."
                )
            if missing_required_ids:
                warnings.append(
                    "Missing staged source identifiers were detected. Family-specific ETL may exclude "
                    "these rows, but exclusions must reconcile explicitly."
                )

            if lab_loinc_null or lab_loinc_blank:
                blockers.append(
                    "LAB_RESULT_CM contains dated rows with NULL/blank LAB_LOINC. The current Measurement "
                    "base transform would not preserve those rows correctly, so Measurement must be "
                    "hardened before the clean build proceeds."
                )

    finally:
        engine.dispose()

    status = "ready_for_phase1" if not blockers else "blocked"
    payload = {
        "stage": "clean_build_preflight",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "database": readiness.get("database"),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "destructive_actions_performed": False,
        "readiness_status": readiness.get("status"),
        "all_core_targets_empty": readiness.get("all_core_targets_empty"),
        "policy_checks": policy_checks,
        "source_identifier_profiles": id_profiles,
        "lab_dated_null_loinc_rows": lab_loinc_null,
        "lab_dated_blank_loinc_rows": lab_loinc_blank,
        "blockers": blockers,
        "warnings": warnings,
        "policy": {
            "purpose": (
                "Read-only source-semantic guard before any clean-build materialization. "
                "No target rows or route ledgers are created or modified."
            ),
            "measurement_null_loinc": (
                "Do not silently drop dated LAB rows because a source code is missing; "
                "either harden the transform to preserve them with concept_id 0 or block the build."
            ),
        },
    }
    audit_path = config.audit_dir / "clean_build_preflight.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only source-semantic preflight for the validated clean build."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    result = audit_clean_build_preflight(load_etl_config(args.config))

    print("status:", result["status"])
    print("database:", result["database"])
    print("source_schema:", result["source_schema"])
    print("target_schema:", result["target_schema"])
    print("destructive_actions_performed:", result["destructive_actions_performed"])
    print("readiness_status:", result["readiness_status"])
    print("all_core_targets_empty:", result["all_core_targets_empty"])
    print("policy_checks:", result["policy_checks"])
    print("lab_dated_null_loinc_rows:", result["lab_dated_null_loinc_rows"])
    print("lab_dated_blank_loinc_rows:", result["lab_dated_blank_loinc_rows"])
    print("source_identifier_profiles:")
    for key, value in result["source_identifier_profiles"].items():
        if value["missing"] or value["duplicate_groups"]:
            print(f"  {key}: {value}")
    print("blockers:")
    for item in result["blockers"]:
        print(" ", item)
    print("warnings:")
    for item in result["warnings"]:
        print(" ", item)
    print("Audit:", result["audit_path"])
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
