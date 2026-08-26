from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_cross_domain_materialize import (
    BASE_LINEAGE,
    DOMAINS,
    ROUTE_TABLE,
    TARGETS,
    XWALK_TABLE,
    materialize_condition_cross_domain_events,
)
from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _component_count(con, schema: str, table: str) -> int:
    if not table_exists(con, schema, table):
        raise RuntimeError(f"Required lineage table [{schema}].[{table}] is missing")
    return _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")


def _base_lineage_counts(con, schema: str) -> dict[str, int]:
    return {
        domain: sum(_component_count(con, schema, table) for table in BASE_LINEAGE[domain])
        for domain in DOMAINS
    }


def _route_counts(con, schema: str) -> dict[str, int]:
    out = {domain: 0 for domain in DOMAINS}
    rows = con.execute(
        text(
            f"""
            SELECT target_domain, COUNT_BIG(*)
            FROM [{schema}].[{ROUTE_TABLE}]
            WHERE is_core_event_route = 1
              AND target_domain IN ('Observation','Procedure','Measurement','Drug','Device','Specimen')
            GROUP BY target_domain
            """
        )
    ).all()
    out.update({str(domain): int(n) for domain, n in rows})
    return out


def _target_counts(con, schema: str) -> dict[str, int]:
    return {
        domain: _scalar(
            con,
            f"SELECT COUNT_BIG(*) FROM [{schema}].[{TARGETS[domain][0]}]",
        )
        for domain in DOMAINS
    }


def _xwalk_counts(con, schema: str) -> dict[str, int]:
    if not table_exists(con, schema, XWALK_TABLE):
        return {domain: 0 for domain in DOMAINS}
    out = {domain: 0 for domain in DOMAINS}
    rows = con.execute(
        text(
            f"""
            SELECT target_domain, COUNT_BIG(*)
            FROM [{schema}].[{XWALK_TABLE}]
            GROUP BY target_domain
            """
        )
    ).all()
    out.update({str(domain): int(n) for domain, n in rows})
    return out


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                ROUTE_TABLE,
                "etl_condition_occurrence_xwalk",
                "etl_obs_clin_condition_xwalk",
                "etl_procedure_condition_xwalk",
                "etl_procedure_occurrence_xwalk",
                "etl_measurement_xwalk",
                "etl_observation_xwalk",
                "etl_drug_exposure_xwalk",
                "etl_device_exposure_xwalk",
                "etl_specimen_xwalk",
            )
            missing = [table for table in required if not table_exists(con, schema, table)]
            if missing:
                raise RuntimeError(f"Phase 10 prerequisite ledgers are missing: {missing}")

            if not table_exists(con, schema, "death"):
                raise RuntimeError(f"Required target [{schema}].[death] is missing")
            death_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[death]")
            if death_rows:
                raise RuntimeError(
                    "Refusing Condition cross-domain phase because Death is already populated: "
                    f"death={death_rows:,}"
                )

            route_counts = _route_counts(con, schema)
            base_counts = _base_lineage_counts(con, schema)
            target_counts = _target_counts(con, schema)
            xwalk_counts = _xwalk_counts(con, schema)
            xwalk_exists = table_exists(con, schema, XWALK_TABLE)

            if not xwalk_exists:
                mismatched = {
                    domain: {"target": target_counts[domain], "base_lineage": base_counts[domain]}
                    for domain in DOMAINS
                    if target_counts[domain] != base_counts[domain]
                }
                if mismatched:
                    raise RuntimeError(
                        "Phase 10 requires pristine pre-append targets fully explained by prior lineage: "
                        f"{mismatched}"
                    )
                status = "ready_for_phase10_condition_cross_domain"
            else:
                route_mismatch = {
                    domain: {"xwalk": xwalk_counts[domain], "route": route_counts[domain]}
                    for domain in DOMAINS
                    if xwalk_counts[domain] != route_counts[domain]
                }
                target_mismatch = {
                    domain: {
                        "target": target_counts[domain],
                        "expected": base_counts[domain] + xwalk_counts[domain],
                    }
                    for domain in DOMAINS
                    if target_counts[domain] != base_counts[domain] + xwalk_counts[domain]
                }
                if route_mismatch or target_mismatch:
                    raise RuntimeError(
                        "Existing Condition cross-domain state is not safely resumable: "
                        f"route_mismatch={route_mismatch}; target_mismatch={target_mismatch}"
                    )
                status = "guarded_phase10_condition_cross_domain_resume"

            return {
                "status": status,
                "route_rows": route_counts,
                "base_lineage_rows": base_counts,
                "target_rows_before": target_counts,
                "cross_domain_xwalk_rows_before": xwalk_counts,
                "death_rows": death_rows,
            }
    finally:
        engine.dispose()


def run_clean_build_phase10_condition_cross_domain(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = materialize_condition_cross_domain_events(config)

    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            route_counts = _route_counts(con, schema)
            base_counts = _base_lineage_counts(con, schema)
            target_counts = _target_counts(con, schema)
            xwalk_counts = _xwalk_counts(con, schema)

            failures: dict[str, object] = {}
            for domain in DOMAINS:
                expected = base_counts[domain] + xwalk_counts[domain]
                if xwalk_counts[domain] != route_counts[domain]:
                    failures[f"{domain}_route_lineage"] = {
                        "route": route_counts[domain],
                        "xwalk": xwalk_counts[domain],
                    }
                if target_counts[domain] != expected:
                    failures[f"{domain}_target_lineage"] = {
                        "target": target_counts[domain],
                        "expected": expected,
                    }

            duplicate_target_ids = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT target_domain, target_row_id
                    FROM [{schema}].[{XWALK_TABLE}]
                    GROUP BY target_domain, target_row_id
                    HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
            duplicate_route_ids = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT route_id
                    FROM [{schema}].[{XWALK_TABLE}]
                    GROUP BY route_id
                    HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
            if duplicate_target_ids or duplicate_route_ids:
                failures["duplicate_lineage_keys"] = {
                    "target_ids": duplicate_target_ids,
                    "route_ids": duplicate_route_ids,
                }
            if failures:
                raise RuntimeError(f"Phase 10 reconciliation failed: {failures}")
    finally:
        engine.dispose()

    payload = {
        "stage": "clean_build_phase10_condition_cross_domain",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase10_condition_cross_domain_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": schema,
        "entry_guard": guard,
        "materialization_result": result,
        "post_counts": {
            "route_rows": route_counts,
            "base_lineage_rows": base_counts,
            "cross_domain_xwalk_rows": xwalk_counts,
            "target_rows": target_counts,
            "duplicate_target_id_groups": duplicate_target_ids,
            "duplicate_route_id_groups": duplicate_route_ids,
        },
    }
    audit_path = config.audit_dir / "clean_build_phase10_condition_cross_domain.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 10 Condition cross-domain materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase10_condition_cross_domain(load_etl_config(args.config))
    m = result["materialization_result"]
    p = result["post_counts"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("materialization_status:", m.get("status"))
    print("route_rows:", p["route_rows"])
    print("cross_domain_xwalk_rows:", p["cross_domain_xwalk_rows"])
    print("base_lineage_rows:", p["base_lineage_rows"])
    print("target_rows:", p["target_rows"])
    print("duplicate_target_id_groups:", p["duplicate_target_id_groups"])
    print("duplicate_route_id_groups:", p["duplicate_route_id_groups"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
