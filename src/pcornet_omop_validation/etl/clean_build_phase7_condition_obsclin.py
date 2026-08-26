from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_obs_clin_append import append_obs_clin_conditions
from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


LATER_TARGETS = ("drug_exposure", "device_exposure", "specimen", "death")


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                "condition_occurrence",
                "etl_condition_occurrence_xwalk",
                "etl_obs_clin_route",
                "observation",
                "etl_observation_xwalk",
                "measurement",
                "etl_measurement_xwalk",
            )
            missing = [t for t in required if not table_exists(con, schema, t)]
            if missing:
                raise RuntimeError(f"Phase 7 prerequisite tables are missing: {missing}")

            observation_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[observation]"
            )
            observation_xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_observation_xwalk]"
            )
            if observation_rows <= 0 or observation_rows != observation_xwalk:
                raise RuntimeError(
                    "Observation prerequisite is not reconciled: "
                    f"target={observation_rows:,}, xwalk={observation_xwalk:,}"
                )

            measurement_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"
            )
            measurement_xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]"
            )
            obsclin_measurement_xwalk = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk] "
                "WHERE source_family='OBS_CLIN'",
            )
            obsclin_measurement_routes = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_route] "
                "WHERE target_domain='Measurement'",
            )
            if measurement_rows != measurement_xwalk or measurement_rows <= 0:
                raise RuntimeError(
                    "Measurement prerequisite is not reconciled: "
                    f"target={measurement_rows:,}, xwalk={measurement_xwalk:,}"
                )
            if obsclin_measurement_xwalk != obsclin_measurement_routes:
                raise RuntimeError(
                    "OBS_CLIN Measurement append is incomplete: "
                    f"routes={obsclin_measurement_routes:,}, "
                    f"lineage={obsclin_measurement_xwalk:,}"
                )

            primary_condition_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_condition_occurrence_xwalk]"
            )
            condition_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
            )
            obs_xwalk_exists = table_exists(con, schema, "etl_obs_clin_condition_xwalk")
            obs_xwalk_rows = (
                _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_condition_xwalk]",
                )
                if obs_xwalk_exists
                else 0
            )
            procedure_xwalk_exists = table_exists(
                con, schema, "etl_procedure_condition_xwalk"
            )
            procedure_xwalk_rows = (
                _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_procedure_condition_xwalk]",
                )
                if procedure_xwalk_exists
                else 0
            )

            expected_current = primary_condition_rows + obs_xwalk_rows + procedure_xwalk_rows
            if condition_rows != expected_current:
                raise RuntimeError(
                    "Condition target is not fully explained by route-aware lineage: "
                    f"target={condition_rows:,}, expected={expected_current:,}, "
                    f"primary={primary_condition_rows:,}, obsclin={obs_xwalk_rows:,}, "
                    f"procedures={procedure_xwalk_rows:,}"
                )
            if procedure_xwalk_rows:
                raise RuntimeError(
                    "Procedure-derived Condition rows already exist; canonical clean-build "
                    "order requires OBS_CLIN Condition before remaining Procedure domains"
                )

            later = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in LATER_TARGETS
            }
            populated_later = {k: v for k, v in later.items() if v != 0}
            if populated_later:
                raise RuntimeError(
                    "Refusing OBS_CLIN Condition phase because later targets are populated: "
                    f"{populated_later}"
                )

            route_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_route] "
                "WHERE target_domain='Condition'",
            )
            return {
                "status": (
                    "guarded_phase7_condition_obsclin_resume"
                    if obs_xwalk_rows
                    else "ready_for_phase7_condition_obsclin"
                ),
                "condition_rows_before": condition_rows,
                "primary_condition_xwalk_rows": primary_condition_rows,
                "obs_clin_condition_xwalk_rows_before": obs_xwalk_rows,
                "obs_clin_condition_route_rows": route_rows,
                "observation_rows": observation_rows,
                "measurement_rows": measurement_rows,
                "later_target_rows": later,
            }
    finally:
        engine.dispose()


def _post_counts(config: EtlConfig) -> dict[str, int]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            return {
                "condition_occurrence": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
                ),
                "primary_condition_xwalk": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_condition_occurrence_xwalk]",
                ),
                "obs_clin_condition_xwalk": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_condition_xwalk]",
                ),
                "obs_clin_condition_routes": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_route] "
                    "WHERE target_domain='Condition'",
                ),
            }
    finally:
        engine.dispose()


def run_clean_build_phase7_condition_obsclin(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = append_obs_clin_conditions(config)
    counts = _post_counts(config)

    expected_total = (
        counts["primary_condition_xwalk"] + counts["obs_clin_condition_xwalk"]
    )
    if counts["obs_clin_condition_xwalk"] != counts["obs_clin_condition_routes"]:
        raise RuntimeError(f"OBS_CLIN Condition route/lineage mismatch: {counts}")
    if counts["condition_occurrence"] != expected_total:
        raise RuntimeError(
            "Condition target does not reconcile after OBS_CLIN append: "
            f"target={counts['condition_occurrence']:,}, expected={expected_total:,}"
        )

    payload = {
        "stage": "clean_build_phase7_condition_obsclin",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase7_condition_obsclin_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "append_result": result,
        "post_counts": counts,
        "next_phase": (
            "Build Drug route ledger and materialize Drug Exposure only after reviewing "
            "OBS_CLIN Condition reconciliation."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase7_condition_obsclin.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 7 OBS_CLIN Condition append."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase7_condition_obsclin(load_etl_config(args.config))
    a = result["append_result"]
    p = result["post_counts"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("append_status:", a.get("status"))
    print("baseline_condition_rows:", a.get("baseline_condition_rows"))
    print("obs_clin_condition_rows:", a.get("obs_clin_condition_rows"))
    print("one_to_many_expansion_rows:", a.get("one_to_many_expansion_rows"))
    print("obs_clin_concept_zero_rows:", a.get("concept_zero_rows"))
    print("visit_linked_rows:", a.get("visit_linked_rows"))
    print("condition_occurrence_rows:", p["condition_occurrence"])
    print("primary_condition_xwalk_rows:", p["primary_condition_xwalk"])
    print("obs_clin_condition_xwalk_rows:", p["obs_clin_condition_xwalk"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
