from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_canonical_routes import (
    ROUTE_TABLE as CONDITION_ROUTE_TABLE,
    materialize_condition_canonical_routes,
)
from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .obs_clin_routes import ROUTE_TABLE as OBSCLIN_ROUTE_TABLE, materialize_obs_clin_routes
from .procedure_event_routes import (
    ROUTE_TABLE as PROCEDURE_ROUTE_TABLE,
    materialize_procedure_event_routes,
)

PHASE1_TARGETS = ("person", "observation_period", "visit_occurrence")
LATER_TARGETS = (
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)
EXPECTED_PHASE2_LEDGERS = (
    PROCEDURE_ROUTE_TABLE,
    OBSCLIN_ROUTE_TABLE,
    CONDITION_ROUTE_TABLE,
)


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            phase1 = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in PHASE1_TARGETS
            }
            later = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in LATER_TARGETS
            }
            visit_xwalk = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_visit_occurrence_xwalk]")
                if table_exists(con, schema, "etl_visit_occurrence_xwalk")
                else None
            )
            phase2_existing = {
                table: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                    if table_exists(con, schema, table)
                    else None
                )
                for table in EXPECTED_PHASE2_LEDGERS
            }
    finally:
        engine.dispose()

    if any(v <= 0 for v in phase1.values()):
        raise RuntimeError(f"Phase 1 is incomplete: {phase1}")
    if visit_xwalk != phase1["visit_occurrence"]:
        raise RuntimeError(
            "Visit lineage is not reconciled: "
            f"visit_occurrence={phase1['visit_occurrence']:,}, xwalk={visit_xwalk}"
        )
    populated_later = {k: v for k, v in later.items() if v != 0}
    if populated_later:
        raise RuntimeError(
            "Refusing route-ledger phase because later OMOP targets already contain rows: "
            f"{populated_later}"
        )
    existing_ledgers = {k: v for k, v in phase2_existing.items() if v is not None}
    if existing_ledgers:
        raise RuntimeError(
            "Refusing route-ledger phase because a Phase 2 ledger already exists. "
            "Do not partially replace ledgers during the clean build: "
            f"{existing_ledgers}"
        )

    return {
        "status": "ready_for_phase2_routes",
        "phase1_target_rows": phase1,
        "visit_xwalk_rows": visit_xwalk,
        "later_target_rows": later,
        "phase2_ledgers_before": phase2_existing,
    }


def _ledger_counts(config: EtlConfig) -> dict[str, int]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            return {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in EXPECTED_PHASE2_LEDGERS
            }
    finally:
        engine.dispose()


def run_clean_build_phase2_routes(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    guard = _guard(config)

    materialize_procedure_event_routes(config_path, replace=False)
    materialize_obs_clin_routes(config_path, replace=False)
    condition_result = materialize_condition_canonical_routes(config_path)

    counts = _ledger_counts(config)
    if any(value <= 0 for value in counts.values()):
        raise RuntimeError(f"Phase 2 route ledger reconciliation failed: {counts}")

    payload = {
        "stage": "clean_build_phase2_routes",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase2_routes_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "route_ledger_rows": counts,
        "condition_canonical_summary": condition_result,
        "core_fact_tables_modified": False,
        "next_phase": "Materialize canonical Condition and Procedure fact rows only after route-ledger review.",
    }
    audit_path = config.audit_dir / "clean_build_phase2_routes.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 2 route-ledger materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase2_routes(args.config)
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("route_ledger_rows:", result["route_ledger_rows"])
    c = result["condition_canonical_summary"]
    print("condition_source_events:", c.get("source_events"))
    print("condition_core_event_route_rows:", c.get("core_event_route_rows"))
    print("condition_fallback_rows:", c.get("fallback_condition_zero_rows"))
    print("condition_multi_core_sources:", c.get("multi_core_route_source_events"))
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
