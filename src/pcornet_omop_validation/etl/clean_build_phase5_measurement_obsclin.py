from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .measurement_obs_clin_append import append_obs_clin_measurements


LATER_TARGETS = (
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                "measurement",
                "etl_measurement_xwalk",
                "etl_obs_clin_route",
                "person",
                "etl_visit_occurrence_xwalk",
                "condition_occurrence",
                "procedure_occurrence",
            )
            missing = [t for t in required if not table_exists(con, schema, t)]
            if missing:
                raise RuntimeError(f"Phase 5 prerequisite tables are missing: {missing}")

            later = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in LATER_TARGETS
            }
            populated_later = {k: v for k, v in later.items() if v != 0}
            if populated_later:
                raise RuntimeError(
                    "Refusing OBS_CLIN Measurement append because later targets are populated: "
                    f"{populated_later}"
                )

            measurement_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"
            )
            xwalk_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]"
            )
            obsclin_xwalk_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk] "
                "WHERE source_family='OBS_CLIN'",
            )
            obsclin_route_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_route] "
                "WHERE target_domain='Measurement'",
            )
            obsclin_route_sources = _scalar(
                con,
                f"SELECT COUNT_BIG(DISTINCT source_obsclin_id) "
                f"FROM [{schema}].[etl_obs_clin_route] WHERE target_domain='Measurement'",
            )
            if obsclin_route_rows != obsclin_route_sources:
                raise RuntimeError(
                    "OBS_CLIN Measurement routes are not one row per source event: "
                    f"routes={obsclin_route_rows:,}, sources={obsclin_route_sources:,}"
                )

            if measurement_rows != xwalk_rows:
                raise RuntimeError(
                    "Measurement target/lineage are not reconciled before Phase 5: "
                    f"target={measurement_rows:,}, xwalk={xwalk_rows:,}"
                )

            overflow_exists = table_exists(
                con, schema, "etl_measurement_obsclin_text_overflow"
            )

            if obsclin_xwalk_rows == 0:
                if overflow_exists:
                    raise RuntimeError(
                        "OBS_CLIN overflow ledger exists without OBS_CLIN Measurement lineage"
                    )
                if measurement_rows <= 0:
                    raise RuntimeError("Base Measurement stage is empty")
                status = "ready_for_phase5_measurement_obsclin"
                baseline_rows = measurement_rows
            else:
                if obsclin_xwalk_rows != obsclin_route_rows:
                    raise RuntimeError(
                        "Existing OBS_CLIN Measurement lineage is partial: "
                        f"xwalk={obsclin_xwalk_rows:,}, routes={obsclin_route_rows:,}"
                    )
                if not overflow_exists:
                    raise RuntimeError(
                        "OBS_CLIN Measurement lineage exists but overflow ledger is missing"
                    )
                baseline_rows = measurement_rows - obsclin_xwalk_rows
                if baseline_rows <= 0:
                    raise RuntimeError(
                        "Cannot derive a positive base Measurement population from existing state"
                    )
                status = "guarded_phase5_measurement_obsclin_resume"

            return {
                "status": status,
                "measurement_rows_before": measurement_rows,
                "measurement_xwalk_rows_before": xwalk_rows,
                "baseline_measurement_rows": baseline_rows,
                "obs_clin_route_rows": obsclin_route_rows,
                "obs_clin_xwalk_rows_before": obsclin_xwalk_rows,
                "overflow_exists_before": overflow_exists,
                "later_target_rows": later,
            }
    finally:
        engine.dispose()


def _post_counts(config: EtlConfig) -> dict[str, int]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            measurement = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"
            )
            xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]"
            )
            obsclin_xwalk = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk] "
                "WHERE source_family='OBS_CLIN'",
            )
            obsclin_routes = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_obs_clin_route] "
                "WHERE target_domain='Measurement'",
            )
            overflow = (
                _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_obsclin_text_overflow]",
                )
                if table_exists(con, schema, "etl_measurement_obsclin_text_overflow")
                else 0
            )
            concept_zero = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement] m "
                f"JOIN [{schema}].[etl_measurement_xwalk] x ON x.measurement_id=m.measurement_id "
                "WHERE x.source_family='OBS_CLIN' AND m.measurement_concept_id=0",
            )
            unit_zero = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement] m "
                f"JOIN [{schema}].[etl_measurement_xwalk] x ON x.measurement_id=m.measurement_id "
                "WHERE x.source_family='OBS_CLIN' AND m.unit_concept_id=0",
            )
            return {
                "measurement": measurement,
                "measurement_xwalk": xwalk,
                "obs_clin_xwalk": obsclin_xwalk,
                "obs_clin_routes": obsclin_routes,
                "obs_clin_overflow": overflow,
                "obs_clin_concept_zero": concept_zero,
                "obs_clin_unit_zero": unit_zero,
            }
    finally:
        engine.dispose()


def run_clean_build_phase5_measurement_obsclin(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = append_obs_clin_measurements(config)
    counts = _post_counts(config)

    if counts["measurement"] != counts["measurement_xwalk"]:
        raise RuntimeError(f"Measurement target/lineage mismatch after Phase 5: {counts}")
    if counts["obs_clin_xwalk"] != counts["obs_clin_routes"]:
        raise RuntimeError(f"OBS_CLIN route/lineage mismatch after Phase 5: {counts}")

    baseline = int(guard["baseline_measurement_rows"])
    expected_total = baseline + counts["obs_clin_routes"]
    if counts["measurement"] != expected_total:
        raise RuntimeError(
            "Measurement total does not equal base plus OBS_CLIN routes: "
            f"base={baseline:,}, routes={counts['obs_clin_routes']:,}, "
            f"target={counts['measurement']:,}"
        )

    payload = {
        "stage": "clean_build_phase5_measurement_obsclin",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase5_measurement_obsclin_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "append_result": result,
        "post_counts": counts,
        "expected_total_measurement_rows": expected_total,
        "next_phase": (
            "Materialize Observation only after reviewing OBS_CLIN Measurement route, "
            "unit, concept-zero, and overflow reconciliation."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase5_measurement_obsclin.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 5 OBS_CLIN Measurement append."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase5_measurement_obsclin(load_etl_config(args.config))
    counts = result["post_counts"]
    append = result["append_result"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("append_status:", append.get("status"))
    print("baseline_measurement_rows:", result["entry_guard"]["baseline_measurement_rows"])
    print("obs_clin_route_rows:", counts["obs_clin_routes"])
    print("obs_clin_xwalk_rows:", counts["obs_clin_xwalk"])
    print("measurement_rows:", counts["measurement"])
    print("measurement_xwalk_rows:", counts["measurement_xwalk"])
    print("obs_clin_concept_zero_rows:", counts["obs_clin_concept_zero"])
    print("obs_clin_unit_zero_rows:", counts["obs_clin_unit_zero"])
    print("obs_clin_overflow_rows:", counts["obs_clin_overflow"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
