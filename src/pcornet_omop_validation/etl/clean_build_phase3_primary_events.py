from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_canonical_routes import ROUTE_TABLE as CONDITION_ROUTE_TABLE
from .condition_occurrence import (
    XWALK_TABLE as CONDITION_XWALK,
    transform_condition_occurrence,
)
from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .obs_clin_routes import ROUTE_TABLE as OBSCLIN_ROUTE_TABLE
from .procedure_event_routes import ROUTE_TABLE as PROCEDURE_ROUTE_TABLE
from .procedure_occurrence import (
    XWALK_TABLE as PROCEDURE_XWALK,
    transform_procedure_occurrence,
)

PHASE1_TARGETS = ("person", "observation_period", "visit_occurrence")
REQUIRED_ROUTE_LEDGERS = (
    PROCEDURE_ROUTE_TABLE,
    OBSCLIN_ROUTE_TABLE,
    CONDITION_ROUTE_TABLE,
)
LATER_TARGETS = (
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _serialize(value: object) -> object:
    return asdict(value) if is_dataclass(value) else value


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            phase1 = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in PHASE1_TARGETS
            }
            route_rows = {}
            for table in REQUIRED_ROUTE_LEDGERS:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required Phase 2 route ledger [{schema}].[{table}] is missing"
                    )
                route_rows[table] = _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]"
                )

            later_rows = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in LATER_TARGETS
            }
            current = {
                "condition_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
                ),
                "procedure_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[procedure_occurrence]"
                ),
            }
            xwalks = {
                CONDITION_XWALK: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{CONDITION_XWALK}]")
                    if table_exists(con, schema, CONDITION_XWALK)
                    else None
                ),
                PROCEDURE_XWALK: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{PROCEDURE_XWALK}]")
                    if table_exists(con, schema, PROCEDURE_XWALK)
                    else None
                ),
            }
    finally:
        engine.dispose()

    if any(value <= 0 for value in phase1.values()):
        raise RuntimeError(f"Phase 1 is incomplete: {phase1}")
    if any(value <= 0 for value in route_rows.values()):
        raise RuntimeError(f"Phase 2 route ledgers are incomplete: {route_rows}")

    populated_later = {k: v for k, v in later_rows.items() if v != 0}
    if populated_later:
        raise RuntimeError(
            "Refusing Phase 3 because later fact tables already contain rows: "
            f"{populated_later}"
        )

    for target, xwalk in (
        ("condition_occurrence", CONDITION_XWALK),
        ("procedure_occurrence", PROCEDURE_XWALK),
    ):
        rows = current[target]
        xrows = xwalks[xwalk]
        if rows == 0 and xrows is not None:
            raise RuntimeError(
                f"Partial Phase 3 state: {target} is empty but [{schema}].[{xwalk}] exists"
            )
        if rows > 0 and xrows != rows:
            raise RuntimeError(
                f"Partial Phase 3 state: {target}={rows:,}, {xwalk}={xrows}"
            )

    status = (
        "guarded_phase3_resume"
        if any(value > 0 for value in current.values())
        else "ready_for_phase3_primary_events"
    )
    return {
        "status": status,
        "phase1_target_rows": phase1,
        "route_ledger_rows": route_rows,
        "primary_target_rows_before": current,
        "primary_xwalk_rows_before": xwalks,
        "later_target_rows": later_rows,
    }


def _post_counts(config: EtlConfig) -> dict[str, int]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            condition_routes = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[{CONDITION_ROUTE_TABLE}]
                WHERE is_core_event_route = 1
                  AND target_domain = 'Condition'
                """,
            )
            procedure_routes = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[{PROCEDURE_ROUTE_TABLE}]
                WHERE target_domain = 'Procedure'
                """,
            )
            counts = {
                "condition_routes": condition_routes,
                "condition_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
                ),
                "condition_xwalk": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{CONDITION_XWALK}]"
                ),
                "procedure_routes": procedure_routes,
                "procedure_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[procedure_occurrence]"
                ),
                "procedure_xwalk": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{PROCEDURE_XWALK}]"
                ),
            }
    finally:
        engine.dispose()
    return counts


def run_clean_build_phase3(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)

    condition_result = transform_condition_occurrence(config)
    procedure_result = transform_procedure_occurrence(config)

    counts = _post_counts(config)
    if not (
        counts["condition_routes"]
        == counts["condition_occurrence"]
        == counts["condition_xwalk"]
    ):
        raise RuntimeError(f"Condition Phase 3 reconciliation failed: {counts}")
    if not (
        counts["procedure_routes"]
        == counts["procedure_occurrence"]
        == counts["procedure_xwalk"]
    ):
        raise RuntimeError(f"Procedure Phase 3 reconciliation failed: {counts}")

    payload = {
        "stage": "clean_build_phase3_primary_events",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase3_primary_events_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "condition": _serialize(condition_result),
        "procedure": _serialize(procedure_result),
        "reconciliation": counts,
        "next_phase": (
            "Materialize base Measurement/Observation and OBS_CLIN append families only "
            "after reviewing primary-event reconciliation."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase3_primary_events.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 3 primary Condition/Procedure materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase3(load_etl_config(args.config))
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("reconciliation:", result["reconciliation"])
    condition = result["condition"]
    procedure = result["procedure"]
    print("condition_status:", condition["status"])
    print("condition_concept_zero:", int(condition["diagnosis_concept_zero"]) + int(condition["condition_concept_zero"]))
    print("procedure_status:", procedure["status"])
    print("procedure_concept_zero:", procedure["concept_zero_rows"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
