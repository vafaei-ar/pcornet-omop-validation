from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .death import transform_death


REQUIRED_LINEAGE = {
    "condition_occurrence": (
        ("etl_condition_occurrence_xwalk", None),
        ("etl_obs_clin_condition_xwalk", None),
        ("etl_procedure_condition_xwalk", None),
    ),
    "procedure_occurrence": (
        ("etl_procedure_occurrence_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Procedure'"),
    ),
    "measurement": (
        ("etl_measurement_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Measurement'"),
    ),
    "observation": (
        ("etl_observation_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Observation'"),
    ),
    "drug_exposure": (
        ("etl_drug_exposure_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Drug'"),
    ),
    "device_exposure": (
        ("etl_device_exposure_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Device'"),
    ),
    "specimen": (
        ("etl_specimen_xwalk", None),
        ("etl_condition_cross_domain_xwalk", "target_domain='Specimen'"),
    ),
}


def _schema(config: EtlConfig) -> str:
    return str(config.raw["sqlserver"].get("target_schema", "dbo"))


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _component_count(con, schema: str, table: str, predicate: str | None) -> int:
    where = f" WHERE {predicate}" if predicate else ""
    return _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]{where}")


def _guard(config: EtlConfig) -> dict[str, object]:
    schema = _schema(config)
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for target, components in REQUIRED_LINEAGE.items():
                if not table_exists(con, schema, target):
                    raise RuntimeError(f"Phase 11 prerequisite target is missing: {target}")
                for table, _ in components:
                    if not table_exists(con, schema, table):
                        raise RuntimeError(f"Phase 11 prerequisite lineage is missing: {table}")

            target_rows: dict[str, int] = {}
            lineage_rows: dict[str, int] = {}
            for target, components in REQUIRED_LINEAGE.items():
                target_n = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{target}]")
                lineage_n = sum(
                    _component_count(con, schema, table, predicate)
                    for table, predicate in components
                )
                target_rows[target] = target_n
                lineage_rows[target] = lineage_n
                if target_n != lineage_n:
                    raise RuntimeError(
                        f"Phase 11 prerequisite reconciliation failed for {target}: "
                        f"target={target_n:,}, lineage={lineage_n:,}"
                    )

            if not table_exists(con, schema, "death"):
                raise RuntimeError("Phase 11 prerequisite target is missing: death")
            death_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[death]")
            death_xwalk_exists = table_exists(con, schema, "etl_death_xwalk")
            death_xwalk_rows = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_death_xwalk]")
                if death_xwalk_exists
                else 0
            )

            if death_rows == 0 and death_xwalk_rows == 0:
                status = "ready_for_phase11_death"
            elif death_rows > 0 and death_xwalk_exists and death_rows == death_xwalk_rows:
                status = "guarded_phase11_death_resume"
            else:
                raise RuntimeError(
                    "Death target/lineage are in a partial state: "
                    f"death={death_rows:,}, xwalk_exists={int(death_xwalk_exists)}, "
                    f"xwalk={death_xwalk_rows:,}"
                )

            return {
                "status": status,
                "target_rows": target_rows,
                "lineage_rows": lineage_rows,
                "death_rows_before": death_rows,
                "death_xwalk_rows_before": death_xwalk_rows,
            }
    finally:
        engine.dispose()


def run_clean_build_phase11_death(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    death_result = transform_death(config)

    schema = _schema(config)
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            death_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[death]")
            lineage_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_death_xwalk]")
            null_date_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{schema}].[death] WHERE death_date IS NULL"
            )
            duplicate_person_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT person_id FROM [{schema}].[death]
                    GROUP BY person_id HAVING COUNT_BIG(*) > 1
                ) q
                """,
            )
    finally:
        engine.dispose()

    if death_rows != lineage_rows:
        raise RuntimeError(
            f"Death final lineage mismatch: target={death_rows:,}, lineage={lineage_rows:,}"
        )
    if null_date_rows or duplicate_person_rows:
        raise RuntimeError(
            "Death final structural checks failed: "
            f"null_date={null_date_rows:,}, duplicate_person_groups={duplicate_person_rows:,}"
        )

    payload = {
        "stage": "clean_build_phase11_death",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase11_death_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": schema,
        "entry_guard": guard,
        "death_result": death_result,
        "post_counts": {
            "death_rows": death_rows,
            "death_xwalk_rows": lineage_rows,
            "null_death_date_rows": null_date_rows,
            "duplicate_person_groups": duplicate_person_rows,
        },
    }
    audit_path = config.audit_dir / "clean_build_phase11_death.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run guarded clean-build Phase 11 Death materialization.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase11_death(load_etl_config(args.config))
    d = result["death_result"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("death_status:", d.get("status"))
    print("source_rows:", d.get("source_rows"))
    print("eligible_rows:", d.get("eligible_rows"))
    print("excluded_missing_patid:", d.get("excluded_missing_patid"))
    print("excluded_missing_death_date:", d.get("excluded_missing_death_date"))
    print("excluded_unlinked_person:", d.get("excluded_unlinked_person"))
    print("death_cause_rows:", d.get("death_cause_rows"))
    print("death_rows:", result["post_counts"]["death_rows"])
    print("death_xwalk_rows:", result["post_counts"]["death_xwalk_rows"])
    print("death_type_concept_zero_rows:", d.get("death_type_concept_zero_rows"))
    print("cause_concept_zero_rows:", d.get("cause_concept_zero_rows"))
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
