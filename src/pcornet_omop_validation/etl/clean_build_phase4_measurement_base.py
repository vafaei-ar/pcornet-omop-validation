from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .measurement import transform_measurement


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
                "condition_occurrence",
                "procedure_occurrence",
                "measurement",
                "etl_condition_occurrence_xwalk",
                "etl_procedure_occurrence_xwalk",
                "etl_procedure_event_route",
                "etl_obs_clin_route",
                "etl_condition_event_route_v2",
                "etl_visit_occurrence_xwalk",
            )
            missing = [t for t in required if not table_exists(con, schema, t)]
            if missing:
                raise RuntimeError(f"Phase 4 prerequisite tables are missing: {missing}")

            condition_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[condition_occurrence]"
            )
            condition_xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_condition_occurrence_xwalk]"
            )
            condition_routes = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_condition_event_route_v2] "
                "WHERE is_core_event_route=1 AND target_domain='Condition'",
            )

            procedure_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[procedure_occurrence]"
            )
            procedure_xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_procedure_occurrence_xwalk]"
            )
            procedure_routes = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_procedure_event_route] "
                "WHERE target_domain='Procedure'",
            )

            if not (
                condition_rows == condition_xwalk == condition_routes
                and condition_rows > 0
            ):
                raise RuntimeError(
                    "Condition prerequisite is not reconciled: "
                    f"routes={condition_routes:,}, target={condition_rows:,}, "
                    f"xwalk={condition_xwalk:,}"
                )
            if not (
                procedure_rows == procedure_xwalk == procedure_routes
                and procedure_rows > 0
            ):
                raise RuntimeError(
                    "Procedure prerequisite is not reconciled: "
                    f"routes={procedure_routes:,}, target={procedure_rows:,}, "
                    f"xwalk={procedure_xwalk:,}"
                )

            later = {
                table: _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
                for table in LATER_TARGETS
            }
            populated_later = {k: v for k, v in later.items() if v != 0}
            if populated_later:
                raise RuntimeError(
                    "Refusing Measurement base phase because later targets are populated: "
                    f"{populated_later}"
                )

            measurement_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"
            )
            measurement_xwalk_exists = table_exists(
                con, schema, "etl_measurement_xwalk"
            )
            measurement_xwalk_rows = (
                _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]",
                )
                if measurement_xwalk_exists
                else None
            )
            obsclin_xwalk_rows = (
                _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk] "
                    "WHERE source_family='OBS_CLIN'",
                )
                if measurement_xwalk_exists
                else 0
            )
            overflow_exists = table_exists(
                con, schema, "etl_measurement_obsclin_text_overflow"
            )

            if obsclin_xwalk_rows:
                raise RuntimeError(
                    "OBS_CLIN Measurement append has already started; Phase 4 base must "
                    "not run after the append stage"
                )
            if overflow_exists:
                raise RuntimeError(
                    "OBS_CLIN Measurement overflow ledger already exists; refusing base phase"
                )
            if measurement_rows and (
                not measurement_xwalk_exists
                or measurement_xwalk_rows != measurement_rows
            ):
                raise RuntimeError(
                    "Existing Measurement rows are not reconciled with base lineage: "
                    f"target={measurement_rows:,}, xwalk={measurement_xwalk_rows}"
                )

            return {
                "status": (
                    "guarded_phase4_measurement_resume"
                    if measurement_rows
                    else "ready_for_phase4_measurement_base"
                ),
                "condition_rows": condition_rows,
                "procedure_rows": procedure_rows,
                "measurement_rows_before": measurement_rows,
                "measurement_xwalk_rows_before": measurement_xwalk_rows,
                "later_target_rows": later,
            }
    finally:
        engine.dispose()


def _post_counts(config: EtlConfig) -> dict[str, int]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            target = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[measurement]"
            )
            xwalk = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk]"
            )
            obsclin = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_measurement_xwalk] "
                "WHERE source_family='OBS_CLIN'",
            )
            return {
                "measurement": target,
                "measurement_xwalk": xwalk,
                "obs_clin_xwalk_rows": obsclin,
            }
    finally:
        engine.dispose()


def run_clean_build_phase4_measurement_base(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = transform_measurement(config)
    counts = _post_counts(config)

    if counts["measurement"] != counts["measurement_xwalk"]:
        raise RuntimeError(f"Measurement base lineage mismatch: {counts}")
    if counts["obs_clin_xwalk_rows"] != 0:
        raise RuntimeError(
            "Measurement base unexpectedly contains OBS_CLIN lineage before append"
        )
    if counts["measurement"] != int(result.target_rows):
        raise RuntimeError(
            "Measurement base result/target mismatch: "
            f"result={result.target_rows:,}, actual={counts['measurement']:,}"
        )

    payload = {
        "stage": "clean_build_phase4_measurement_base",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase4_measurement_base_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "measurement_result": {
            "status": result.status,
            "expected_rows": int(result.expected_rows),
            "target_rows": int(result.target_rows),
            "lineage_rows": int(result.lineage_rows),
            "lab_measurement_rows": int(result.lab_measurement_rows),
            "vital_measurement_rows": int(result.vital_measurement_rows),
            "procedure_measurement_rows": int(result.procedure_measurement_rows),
            "lab_observation_domain_rows": int(result.lab_observation_domain_rows),
            "target_concept_zero_rows": int(result.target_concept_zero_rows),
            "lab_unit_concept_zero_rows": int(result.lab_unit_concept_zero_rows),
        },
        "post_counts": counts,
        "next_phase": (
            "Append OBS_CLIN Measurement routes only after reviewing base Measurement "
            "reconciliation and source-derived mapping counts."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase4_measurement_base.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 4 Measurement base materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase4_measurement_base(load_etl_config(args.config))
    m = result["measurement_result"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("measurement_status:", m["status"])
    print("expected_rows:", m["expected_rows"])
    print("target_rows:", m["target_rows"])
    print("lineage_rows:", m["lineage_rows"])
    print("lab_measurement_rows:", m["lab_measurement_rows"])
    print("vital_measurement_rows:", m["vital_measurement_rows"])
    print("procedure_measurement_rows:", m["procedure_measurement_rows"])
    print("lab_observation_domain_rows:", m["lab_observation_domain_rows"])
    print("target_concept_zero_rows:", m["target_concept_zero_rows"])
    print("lab_unit_concept_zero_rows:", m["lab_unit_concept_zero_rows"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
