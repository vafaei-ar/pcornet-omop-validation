from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


# Canonical clean-build order. This is deliberately code-defined rather than
# inferred from a site-local config so dependency order cannot drift silently.
CANONICAL_STAGE_ORDER = (
    "schema",
    "vocabulary",
    "staging",
    "person",
    "observation_period",
    "visit_occurrence",
    "procedure_event_routes",
    "obs_clin_routes",
    "condition_canonical_routes",
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "measurement_obs_clin_append",
    "observation",
    "condition_obs_clin_append",
    "drug_event_routes",
    "drug_exposure",
    "drug_route_finalize",
    "procedure_remaining_domains",
    "condition_cross_domain_materialize",
    "death",
    "global_reconciliation",
    "semantic_freeze_audit",
    "freeze_decision_review",
)

CORE_TARGETS = (
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)

NEW_ROUTE_AWARE_LEDGERS = (
    "etl_condition_event_route_v2",
    "etl_condition_occurrence_xwalk",
    "etl_obs_clin_condition_xwalk",
    "etl_procedure_condition_xwalk",
    "etl_condition_cross_domain_xwalk",
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def audit_rebuild_readiness(config: EtlConfig) -> dict[str, object]:
    """Read-only audit of clean-rebuild safety and stage-order dependencies.

    This function never creates, drops, truncates, deletes, or updates database
    objects. Destructive reset semantics remain isolated in clean_reset.py.
    """
    sql_cfg = config.raw["sqlserver"]
    etl_cfg = config.raw.get("etl", {}) or {}
    database = str(sql_cfg.get("database") or "").strip()
    if not database:
        raise RuntimeError("sqlserver.database is empty")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    configured_stages = tuple(config.stages)
    # Use the actual public configuration keys from config/etl.example.yaml.
    reset_flag = bool(etl_cfg.get("reset_target", False))
    fail_on_existing = etl_cfg.get("fail_on_existing_target_rows")
    fail_on_missing = etl_cfg.get("fail_on_missing_required_table")

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            target_rows: dict[str, int | None] = {}
            for table in CORE_TARGETS:
                target_rows[table] = (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]")
                    if table_exists(con, target_schema, table)
                    else None
                )

            ledgers = {
                table: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]")
                    if table_exists(con, target_schema, table)
                    else None
                )
                for table in NEW_ROUTE_AWARE_LEDGERS
            }

            staging_tables = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = '{target_schema}'
                  AND TABLE_NAME LIKE 'PCORnet[_]%'
                """,
            )
            concept_rows = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[concept]")
                if table_exists(con, target_schema, "concept")
                else 0
            )
    finally:
        engine.dispose()

    populated_core = {
        k: int(v) for k, v in target_rows.items() if v is not None and int(v) > 0
    }
    missing_core = [k for k, v in target_rows.items() if v is None]
    all_core_empty = not populated_core and not missing_core

    blockers: list[str] = []
    warnings: list[str] = []

    if reset_flag:
        blockers.append(
            "etl.reset_target is true. Clean-build reset must remain a separate explicit operation; "
            "ordinary ETL execution must not contain an implicit destructive reset."
        )
    if fail_on_existing is False:
        warnings.append(
            "etl.fail_on_existing_target_rows is false; freeze-candidate execution should refuse accidental append/overwrite states."
        )
    if fail_on_existing is None:
        warnings.append(
            "etl.fail_on_existing_target_rows is not configured explicitly."
        )
    if fail_on_missing is False:
        warnings.append(
            "etl.fail_on_missing_required_table is false; freeze-candidate execution should fail on missing required inputs."
        )
    if fail_on_missing is None:
        warnings.append(
            "etl.fail_on_missing_required_table is not configured explicitly."
        )
    if missing_core:
        warnings.append("OMOP core schema is incomplete: " + ", ".join(missing_core))
    if concept_rows == 0:
        blockers.append("Vocabulary concept table is absent or empty.")
    if staging_tables == 0:
        blockers.append("No PCORnet staging tables were detected in the configured target schema.")

    if populated_core:
        warnings.append(
            "Core OMOP targets are populated. Do not run route-aware materializers in-place; "
            "use the separately guarded clean reset first."
        )

    # Configured stage lists are documentation until they match the code-defined
    # dependency-safe order exactly. A mismatch does not make the empty database
    # unsafe, but the clean-build runner must use CANONICAL_STAGE_ORDER directly.
    stage_order_matches = configured_stages == CANONICAL_STAGE_ORDER
    if configured_stages and not stage_order_matches:
        warnings.append(
            "Configured stages do not exactly match the canonical dependency-safe clean-build order."
        )

    if blockers:
        status = "blocked"
    elif all_core_empty:
        status = "ready_for_clean_build"
    else:
        status = "ready_for_guarded_reset"

    payload = {
        "stage": "rebuild_readiness",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "database": database,
        "target_schema": target_schema,
        "destructive_actions_performed": False,
        "etl_reset_flag": reset_flag,
        "fail_on_existing": fail_on_existing,
        "fail_on_missing": fail_on_missing,
        "configured_stage_order": list(configured_stages),
        "canonical_stage_order": list(CANONICAL_STAGE_ORDER),
        "stage_order_matches": stage_order_matches,
        "target_rows": target_rows,
        "all_core_targets_empty": all_core_empty,
        "new_route_aware_ledgers": ledgers,
        "pcornet_staging_table_count": staging_tables,
        "concept_rows": concept_rows,
        "blockers": blockers,
        "warnings": warnings,
        "policy": {
            "ordinary_schema_stage": "non-destructive",
            "ordinary_etl": "must never implicitly drop or reset the configured database",
            "clean_reset": (
                "separate explicit command requiring the operator to repeat the exact configured database and schema"
            ),
            "prior_comparator": (
                "never inferred, discovered, dropped, truncated, or modified by the validated-target reset path"
            ),
        },
    }

    audit_path = config.audit_dir / "rebuild_readiness.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of clean-rebuild safety and dependency order."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    result = audit_rebuild_readiness(load_etl_config(args.config))

    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("destructive_actions_performed:", result["destructive_actions_performed"])
    print("etl_reset_flag:", result["etl_reset_flag"])
    print("fail_on_existing:", result["fail_on_existing"])
    print("fail_on_missing:", result["fail_on_missing"])
    print("stage_order_matches:", result["stage_order_matches"])
    print("all_core_targets_empty:", result["all_core_targets_empty"])
    print("pcornet_staging_table_count:", result["pcornet_staging_table_count"])
    print("concept_rows:", result["concept_rows"])
    print("new_route_aware_ledgers:", result["new_route_aware_ledgers"])
    print("blockers:")
    for item in result["blockers"]:
        print(" ", item)
    print("warnings:")
    for item in result["warnings"]:
        print(" ", item)
    print("canonical_stage_order:")
    for index, stage in enumerate(result["canonical_stage_order"], start=1):
        print(f"  {index:02d}. {stage}")
    print("Audit:", result["audit_path"])
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
