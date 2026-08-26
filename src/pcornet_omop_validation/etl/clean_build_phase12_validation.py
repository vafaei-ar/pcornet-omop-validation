from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .global_reconciliation import reconcile_validated_etl
from .semantic_freeze_audit import audit_semantic_freeze
from .visit_time_semantics_audit import audit_visit_time_semantics


REQUIRED_TARGETS = (
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

REQUIRED_LEDGERS = (
    "etl_visit_occurrence_xwalk",
    "etl_condition_occurrence_xwalk",
    "etl_obs_clin_condition_xwalk",
    "etl_procedure_condition_xwalk",
    "etl_procedure_occurrence_xwalk",
    "etl_measurement_xwalk",
    "etl_observation_xwalk",
    "etl_drug_exposure_xwalk",
    "etl_device_exposure_xwalk",
    "etl_specimen_xwalk",
    "etl_death_xwalk",
    "etl_condition_cross_domain_xwalk",
    "etl_condition_event_route_v2",
    "etl_procedure_event_route",
    "etl_obs_clin_route",
    "etl_drug_event_route",
)


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            missing_targets = [t for t in REQUIRED_TARGETS if not table_exists(con, schema, t)]
            missing_ledgers = [t for t in REQUIRED_LEDGERS if not table_exists(con, schema, t)]
            if missing_targets or missing_ledgers:
                raise RuntimeError(
                    f"Phase 12 prerequisites missing: targets={missing_targets}, ledgers={missing_ledgers}"
                )

            target_rows = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in REQUIRED_TARGETS
            }
            empty = {k: v for k, v in target_rows.items() if v == 0}
            if empty:
                raise RuntimeError(f"Phase 12 requires completed target tables; empty={empty}")

            return {
                "status": "ready_for_phase12_validation",
                "target_rows": target_rows,
            }
    finally:
        engine.dispose()


def run_clean_build_phase12_validation(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)

    visit_time = audit_visit_time_semantics(config)
    if visit_time.get("status") != "matched":
        raise RuntimeError(
            "Visit time semantics audit blocked final validation: "
            f"{visit_time.get('hard_blockers')}"
        )

    global_result = reconcile_validated_etl(config)
    if global_result.get("status") != "matched":
        raise RuntimeError(
            f"Global reconciliation did not match: {global_result.get('status')}"
        )

    semantic_result = audit_semantic_freeze(config)
    if semantic_result.get("status") == "blocked":
        raise RuntimeError(
            "Semantic freeze audit has hard blockers: "
            f"{semantic_result.get('hard_blockers')}"
        )

    payload = {
        "stage": "clean_build_phase12_validation",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase12_validation_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "visit_time_semantics": visit_time,
        "global_reconciliation": global_result,
        "semantic_freeze": semantic_result,
        "freeze_candidate_status": (
            "review_required"
            if semantic_result.get("status") == "review_required"
            else "validation_matched"
        ),
    }

    audit_path = config.audit_dir / "clean_build_phase12_validation.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded final structural, lineage, time-semantic, and concept validation."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase12_validation(load_etl_config(args.config))
    v = result["visit_time_semantics"]
    g = result["global_reconciliation"]
    s = result["semantic_freeze"]

    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("visit_time_semantics_status:", v.get("status"))
    print("admit_time_sql_type:", v.get("admit_time_sql_type"))
    print("discharge_time_sql_type:", v.get("discharge_time_sql_type"))
    print("visit_time_interpretations:", v.get("interpretations"))
    print("admit_time_profile:", v.get("admit_time_profile"))
    print("discharge_time_profile:", v.get("discharge_time_profile"))
    print("materialized_datetime_mismatch_rows:", v.get("materialized_datetime_mismatch_rows"))
    print("global_reconciliation_status:", g.get("status"))
    print("target_rows:", g.get("target_rows"))
    print("duplicate_primary_keys:", g.get("duplicate_primary_keys"))
    print("reversed_intervals:", g.get("reversed_intervals"))
    print("semantic_freeze_status:", s.get("status"))
    print("semantic_hard_blockers:", s.get("hard_blockers"))
    print("semantic_review_flags:", s.get("review_flags"))
    print("freeze_candidate_status:", result["freeze_candidate_status"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
