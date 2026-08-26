from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .procedure_remaining_domains import materialize_procedure_remaining_domains


ROUTE_TABLE = "etl_procedure_event_route"
PRIMARY_CONDITION_XWALK = "etl_condition_occurrence_xwalk"
OBSCLIN_CONDITION_XWALK = "etl_obs_clin_condition_xwalk"
PROCEDURE_CONDITION_XWALK = "etl_procedure_condition_xwalk"
DEVICE_XWALK = "etl_device_exposure_xwalk"
SPECIMEN_XWALK = "etl_specimen_xwalk"


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _rows_if_exists(con, schema: str, table: str) -> int:
    if not table_exists(con, schema, table):
        return 0
    return _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")


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
                "drug_exposure",
                "device_exposure",
                "specimen",
                ROUTE_TABLE,
                PRIMARY_CONDITION_XWALK,
                OBSCLIN_CONDITION_XWALK,
                "etl_drug_exposure_xwalk",
            )
            missing = [t for t in required if not table_exists(con, schema, t)]
            if missing:
                raise RuntimeError(f"Phase 9 prerequisite tables are missing: {missing}")

            drug_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[drug_exposure]")
            drug_xwalk = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_drug_exposure_xwalk]")
            if drug_rows <= 0 or drug_xwalk != drug_rows:
                raise RuntimeError(
                    "Drug phase is not complete/reconciled before Phase 9: "
                    f"drug_exposure={drug_rows:,}, lineage={drug_xwalk:,}"
                )

            death_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[death]")
            if death_rows:
                raise RuntimeError(
                    f"Refusing Phase 9 because death is already populated: {death_rows:,}"
                )

            cross_domain_xwalks = (
                "etl_condition_observation_xwalk",
                "etl_condition_procedure_xwalk",
                "etl_condition_measurement_xwalk",
                "etl_condition_drug_xwalk",
                "etl_condition_device_xwalk",
                "etl_condition_specimen_xwalk",
            )
            present_cross = {
                t: _rows_if_exists(con, schema, t)
                for t in cross_domain_xwalks
                if table_exists(con, schema, t)
            }
            if present_cross:
                raise RuntimeError(
                    "Refusing Phase 9 because Condition cross-domain lineage already exists: "
                    f"{present_cross}"
                )

            condition_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
            )
            primary_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{PRIMARY_CONDITION_XWALK}]"
            )
            obsclin_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{OBSCLIN_CONDITION_XWALK}]"
            )
            procedure_condition_rows = _rows_if_exists(
                con, schema, PROCEDURE_CONDITION_XWALK
            )
            device_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[device_exposure]"
            )
            specimen_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[specimen]"
            )
            device_xwalk_rows = _rows_if_exists(con, schema, DEVICE_XWALK)
            specimen_xwalk_rows = _rows_if_exists(con, schema, SPECIMEN_XWALK)

            route_counts = {
                domain: _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[{ROUTE_TABLE}] "
                    f"WHERE target_domain='{domain}'",
                )
                for domain in ("Condition", "Device", "Specimen")
            }

            pristine = (
                procedure_condition_rows == 0
                and device_rows == 0
                and specimen_rows == 0
                and device_xwalk_rows == 0
                and specimen_xwalk_rows == 0
                and condition_rows == primary_rows + obsclin_rows
            )
            matched = (
                procedure_condition_rows == route_counts["Condition"]
                and device_rows == route_counts["Device"]
                and specimen_rows == route_counts["Specimen"]
                and device_xwalk_rows == route_counts["Device"]
                and specimen_xwalk_rows == route_counts["Specimen"]
                and condition_rows
                    == primary_rows + obsclin_rows + procedure_condition_rows
            )
            if not pristine and not matched:
                raise RuntimeError(
                    "Phase 9 state is neither pristine nor a reconciled prior Phase 9 result: "
                    f"condition={condition_rows:,}, primary={primary_rows:,}, "
                    f"obsclin={obsclin_rows:,}, procedure_condition={procedure_condition_rows:,}, "
                    f"device={device_rows:,}, specimen={specimen_rows:,}"
                )

            return {
                "status": (
                    "ready_for_phase9_procedure_remaining"
                    if pristine
                    else "guarded_phase9_procedure_remaining_resume"
                ),
                "condition_rows_before": condition_rows,
                "primary_condition_xwalk_rows": primary_rows,
                "obsclin_condition_xwalk_rows": obsclin_rows,
                "procedure_condition_xwalk_rows_before": procedure_condition_rows,
                "device_rows_before": device_rows,
                "specimen_rows_before": specimen_rows,
                "route_counts": route_counts,
                "drug_exposure_rows": drug_rows,
            }
    finally:
        engine.dispose()


def run_clean_build_phase9_procedure_remaining(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = materialize_procedure_remaining_domains(config)

    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            post = {
                "condition_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
                ),
                "procedure_condition_xwalk": _rows_if_exists(
                    con, schema, PROCEDURE_CONDITION_XWALK
                ),
                "device_exposure": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[device_exposure]"
                ),
                "device_xwalk": _rows_if_exists(con, schema, DEVICE_XWALK),
                "specimen": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[specimen]"
                ),
                "specimen_xwalk": _rows_if_exists(con, schema, SPECIMEN_XWALK),
            }
            duplicate_pk = {
                "condition_occurrence": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT condition_occurrence_id
                      FROM [{schema}].[condition_occurrence]
                      GROUP BY condition_occurrence_id HAVING COUNT_BIG(*)>1
                    ) q
                    """,
                ),
                "device_exposure": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT device_exposure_id
                      FROM [{schema}].[device_exposure]
                      GROUP BY device_exposure_id HAVING COUNT_BIG(*)>1
                    ) q
                    """,
                ),
                "specimen": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT specimen_id FROM [{schema}].[specimen]
                      GROUP BY specimen_id HAVING COUNT_BIG(*)>1
                    ) q
                    """,
                ),
            }
    finally:
        engine.dispose()

    if any(duplicate_pk.values()):
        raise RuntimeError(f"Phase 9 duplicate primary keys found: {duplicate_pk}")

    payload = {
        "stage": "clean_build_phase9_procedure_remaining",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase9_procedure_remaining_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": schema,
        "entry_guard": guard,
        "materialization": result,
        "post_counts": post,
        "duplicate_pk_groups": duplicate_pk,
    }
    audit_path = config.audit_dir / "clean_build_phase9_procedure_remaining.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 9 remaining Procedure domains."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase9_procedure_remaining(load_etl_config(args.config))
    m = result["materialization"]
    p = result["post_counts"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("materialization_status:", m.get("status"))
    print("route_rows:", m.get("route_rows"))
    print("distinct_source_events:", m.get("distinct_source_events"))
    print("one_to_many_expansion:", m.get("one_to_many_expansion"))
    print("concept_zero_rows:", m.get("concept_zero_rows"))
    print("condition_occurrence_rows:", p["condition_occurrence"])
    print("procedure_condition_xwalk_rows:", p["procedure_condition_xwalk"])
    print("device_exposure_rows:", p["device_exposure"])
    print("device_xwalk_rows:", p["device_xwalk"])
    print("specimen_rows:", p["specimen"])
    print("specimen_xwalk_rows:", p["specimen_xwalk"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
