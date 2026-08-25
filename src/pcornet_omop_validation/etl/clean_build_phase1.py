from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from .clean_build_preflight import audit_clean_build_preflight
from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .observation_period import transform_observation_period
from .person import transform_person
from .visit_occurrence import transform_visit_occurrence


PHASE1_TARGETS = (
    "person",
    "observation_period",
    "visit_occurrence",
)

POST_PHASE1_TARGETS = (
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)


def _serializable(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    return value


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _target_counts(config: EtlConfig) -> dict[str, int | None]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            return {
                table: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                    if table_exists(con, schema, table)
                    else None
                )
                for table in PHASE1_TARGETS
            }
    finally:
        engine.dispose()


def _resume_guard(config: EtlConfig) -> dict[str, object]:
    """Verify a partially completed Phase 1 can be resumed without reset.

    A resume is allowed only when no post-Phase-1 clinical target has rows and
    no ETL-owned ledger exists except the Visit xwalk, which itself is a Phase 1
    object. This prevents a convenient resume path from becoming an accidental
    append path for later stages.
    """
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            later_rows = {
                table: (
                    _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                    if table_exists(con, schema, table)
                    else None
                )
                for table in POST_PHASE1_TARGETS
            }
            etl_tables = [
                str(row[0])
                for row in con.execute(
                    text(
                        """
                        SELECT TABLE_NAME
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_SCHEMA = :schema
                          AND TABLE_TYPE = 'BASE TABLE'
                          AND TABLE_NAME LIKE 'etl[_]%'
                        ORDER BY TABLE_NAME
                        """
                    ),
                    {"schema": schema},
                ).fetchall()
            ]
    finally:
        engine.dispose()

    nonzero_later = {
        table: rows
        for table, rows in later_rows.items()
        if rows not in (0, None)
    }
    disallowed_ledgers = [
        table for table in etl_tables if table != "etl_visit_occurrence_xwalk"
    ]
    if nonzero_later or disallowed_ledgers:
        raise RuntimeError(
            "Refusing Phase 1 resume because later-stage materialization is present: "
            f"nonzero_later_targets={nonzero_later}, "
            f"disallowed_etl_tables={disallowed_ledgers}"
        )

    return {
        "status": "guarded_phase1_resume",
        "post_phase1_target_rows": later_rows,
        "etl_tables": etl_tables,
    }


def run_clean_build_phase1(config: EtlConfig) -> dict[str, object]:
    """Run or safely resume the first materialization phase of the clean build.

    Phase 1 is deliberately limited to Person, Observation Period, and Visit
    Occurrence. If an early Phase 1 stage succeeded before a later Phase 1 stage
    failed, the runner may resume only after proving no later ETL stage has run.
    """
    before = _target_counts(config)
    empty_entry = all(value in (0, None) for value in before.values())

    if empty_entry:
        preflight = audit_clean_build_preflight(config)
        if preflight.get("status") != "ready_for_phase1":
            raise RuntimeError(
                "Clean-build preflight is not ready_for_phase1; refusing materialization"
            )
        entry_guard: dict[str, object] = {
            "status": str(preflight.get("status")),
        }
    else:
        entry_guard = _resume_guard(config)

    person = transform_person(config)
    observation_period = transform_observation_period(config)
    visit_occurrence = transform_visit_occurrence(config)

    after = _target_counts(config)
    expected = {
        "person": int(person.target_rows),
        "observation_period": int(observation_period.target_rows),
        "visit_occurrence": int(visit_occurrence.target_rows),
    }
    if after != expected:
        raise RuntimeError(
            "Phase 1 post-materialization reconciliation failed: "
            f"expected={expected}, actual={after}"
        )

    payload = {
        "stage": "clean_build_phase1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase1_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": entry_guard,
        "preflight_status": entry_guard.get("status"),
        "before_target_rows": before,
        "after_target_rows": after,
        "person": _serializable(person),
        "observation_period": _serializable(observation_period),
        "visit_occurrence": _serializable(visit_occurrence),
        "next_phase": (
            "Build procedure, OBS_CLIN, and canonical Condition route ledgers only "
            "after reviewing this phase-1 reconciliation."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase1.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build phase 1: person, observation period, visit."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase1(load_etl_config(args.config))
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("preflight_status:", result["preflight_status"])
    print("before_target_rows:", result["before_target_rows"])
    print("after_target_rows:", result["after_target_rows"])
    print("person_status:", result["person"]["status"])
    print("observation_period_status:", result["observation_period"]["status"])
    print("visit_occurrence_status:", result["visit_occurrence"]["status"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
