from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .drug_event_routes import build_drug_event_routes
from .drug_exposure import transform_drug_exposure
from .drug_route_finalize import finalize_drug_routes


ROUTE_TABLE = "etl_drug_event_route"
XWALK_TABLE = "etl_drug_exposure_xwalk"


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                "condition_occurrence",
                "procedure_occurrence",
                "measurement",
                "observation",
                "etl_condition_occurrence_xwalk",
                "etl_obs_clin_condition_xwalk",
                "etl_measurement_xwalk",
                "etl_observation_xwalk",
                "etl_procedure_event_route",
            )
            missing = [t for t in required if not table_exists(con, schema, t)]
            if missing:
                raise RuntimeError(f"Phase 8 prerequisite tables are missing: {missing}")

            counts = {
                "condition_occurrence": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"),
                "procedure_occurrence": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[procedure_occurrence]"),
                "measurement": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"),
                "observation": _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[observation]"),
            }
            if any(v <= 0 for v in counts.values()):
                raise RuntimeError(f"Phase 8 prerequisites are not populated: {counts}")

            if _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]") != counts["measurement"]:
                raise RuntimeError("Measurement is not reconciled with its lineage")
            if _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_observation_xwalk]") != counts["observation"]:
                raise RuntimeError("Observation is not reconciled with its lineage")

            primary_condition = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_condition_occurrence_xwalk]")
            obsclin_condition = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_condition_xwalk]")
            if primary_condition + obsclin_condition != counts["condition_occurrence"]:
                raise RuntimeError(
                    "Condition is not reconciled before Drug phase: "
                    f"target={counts['condition_occurrence']:,}, "
                    f"primary={primary_condition:,}, obsclin={obsclin_condition:,}"
                )

            later_targets = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in ("device_exposure", "specimen", "death")
            }
            populated_later = {k: v for k, v in later_targets.items() if v}
            if populated_later:
                raise RuntimeError(
                    "Refusing Drug phase because later targets are already populated: "
                    f"{populated_later}"
                )

            drug_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure]")
            route_exists = table_exists(con, schema, ROUTE_TABLE)
            xwalk_exists = table_exists(con, schema, XWALK_TABLE)
            route_rows = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{ROUTE_TABLE}]")
                if route_exists else 0
            )
            xwalk_rows = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{XWALK_TABLE}]")
                if xwalk_exists else 0
            )

            if drug_rows:
                if not route_exists or not xwalk_exists:
                    raise RuntimeError("Drug Exposure rows exist without route/lineage tables")
                if xwalk_rows != drug_rows:
                    raise RuntimeError(
                        f"Drug Exposure lineage mismatch before resume: target={drug_rows:,}, xwalk={xwalk_rows:,}"
                    )
                status = "guarded_phase8_drug_resume"
            else:
                if xwalk_rows:
                    raise RuntimeError("Drug lineage exists while drug_exposure is empty")
                status = "ready_for_phase8_drug"

            return {
                "status": status,
                "prerequisite_rows": counts,
                "drug_rows_before": drug_rows,
                "route_rows_before": route_rows,
                "xwalk_rows_before": xwalk_rows,
                "later_target_rows": later_targets,
            }
    finally:
        engine.dispose()


def run_clean_build_phase8_drug(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)

    if int(guard["drug_rows_before"]) == 0:
        route_result = build_drug_event_routes(config)
    else:
        route_result = {
            "status": "existing_route_ledger_reused",
            "route_rows": int(guard["route_rows_before"]),
        }

    transform_result = transform_drug_exposure(config)
    route_finalize_result = finalize_drug_routes(config)

    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            route_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{ROUTE_TABLE}]")
            drug_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure]")
            xwalk_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{XWALK_TABLE}]")
            duplicate_pk = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT drug_exposure_id
                    FROM [{schema}].[drug_exposure]
                    GROUP BY drug_exposure_id
                    HAVING COUNT_BIG(*) > 1
                ) q
            """)
            null_start = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure] WHERE drug_exposure_start_date IS NULL")
            null_end = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure] WHERE drug_exposure_end_date IS NULL")
            reversed_intervals = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure]
                WHERE drug_exposure_end_date < drug_exposure_start_date
            """)
    finally:
        engine.dispose()

    if drug_rows != xwalk_rows:
        raise RuntimeError(
            f"Drug final lineage mismatch: target={drug_rows:,}, xwalk={xwalk_rows:,}"
        )
    if duplicate_pk or null_start or null_end or reversed_intervals:
        raise RuntimeError(
            "Drug final structural checks failed: "
            f"duplicate_pk={duplicate_pk:,}, null_start={null_start:,}, "
            f"null_end={null_end:,}, reversed={reversed_intervals:,}"
        )

    payload = {
        "stage": "clean_build_phase8_drug",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase8_drug_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": schema,
        "entry_guard": guard,
        "route_result": route_result,
        "drug_result": transform_result,
        "route_finalize_result": route_finalize_result,
        "post_counts": {
            "route_rows": route_rows,
            "drug_exposure_rows": drug_rows,
            "drug_exposure_xwalk_rows": xwalk_rows,
            "duplicate_pk_groups": duplicate_pk,
            "null_start_date_rows": null_start,
            "null_end_date_rows": null_end,
            "end_before_start_rows": reversed_intervals,
        },
    }
    audit_path = config.audit_dir / "clean_build_phase8_drug.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 8 Drug routing/materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase8_drug(load_etl_config(args.config))
    d = result["drug_result"]
    f = result["route_finalize_result"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("route_status:", result["route_result"].get("status"))
    print("route_rows:", result["post_counts"]["route_rows"])
    print("drug_status:", d.get("status"))
    print("eligible_route_rows:", d.get("eligible_route_rows"))
    print("excluded_route_rows:", d.get("excluded_route_rows"))
    print("drug_exposure_rows:", result["post_counts"]["drug_exposure_rows"])
    print("drug_exposure_xwalk_rows:", result["post_counts"]["drug_exposure_xwalk_rows"])
    print("drug_concept_zero_rows:", d.get("concept_zero_rows"))
    print("visit_linked_rows:", d.get("visit_linked_rows"))
    print("route_finalize_status:", f.get("status"))
    print("standardized_route_rows:", f.get("standardized_route_rows"))
    print("mapped_route_rows:", f.get("mapped_rows"))
    print("remaining_standardized_route_zero_rows:", f.get("remaining_standardized_zero_rows"))
    print("all_route_concept_zero_rows:", f.get("all_route_concept_zero_rows"))
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
