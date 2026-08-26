from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .observation import transform_observation


LATER_TARGETS = (
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _guard(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    policies = config.raw.get("policies", {}) or {}

    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "Observation phase requires policies.missing_required_date=exclude"
        )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            target_required = (
                "measurement",
                "etl_measurement_xwalk",
                "etl_measurement_obsclin_text_overflow",
                "observation",
                "person",
                "etl_visit_occurrence_xwalk",
                "etl_obs_clin_route",
                "etl_procedure_event_route",
                "concept",
            )
            source_required = (
                "PCORnet_OBS_CLIN",
                "PCORnet_OBS_GEN",
                "PCORnet_LAB_RESULT_CM",
                "PCORnet_PROCEDURES",
                "PCORnet_VITAL",
            )
            missing = [
                f"{source_schema}.{t}"
                for t in source_required
                if not table_exists(con, source_schema, t)
            ] + [
                f"{target_schema}.{t}"
                for t in target_required
                if not table_exists(con, target_schema, t)
            ]
            if missing:
                raise RuntimeError(f"Phase 6 prerequisite tables are missing: {missing}")

            measurement_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[measurement]"
            )
            measurement_xwalk_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_measurement_xwalk]",
            )
            obsclin_measurement_routes = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_obs_clin_route] "
                "WHERE target_domain='Measurement'",
            )
            obsclin_measurement_xwalk = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[etl_measurement_xwalk] "
                "WHERE source_family='OBS_CLIN'",
            )
            if measurement_rows <= 0 or measurement_rows != measurement_xwalk_rows:
                raise RuntimeError(
                    "Measurement prerequisite is not reconciled: "
                    f"target={measurement_rows:,}, xwalk={measurement_xwalk_rows:,}"
                )
            if obsclin_measurement_xwalk != obsclin_measurement_routes:
                raise RuntimeError(
                    "OBS_CLIN Measurement append is incomplete: "
                    f"routes={obsclin_measurement_routes:,}, "
                    f"xwalk={obsclin_measurement_xwalk:,}"
                )

            observation_rows = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[observation]"
            )
            observation_xwalk_exists = table_exists(
                con, target_schema, "etl_observation_xwalk"
            )
            overflow_exists = table_exists(
                con, target_schema, "etl_observation_text_overflow"
            )
            if observation_rows or observation_xwalk_exists or overflow_exists:
                raise RuntimeError(
                    "Observation phase requires a pristine Observation target/lineage state: "
                    f"target_rows={observation_rows:,}, "
                    f"xwalk_exists={observation_xwalk_exists}, "
                    f"overflow_exists={overflow_exists}"
                )

            later = {
                table: _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]"
                )
                for table in LATER_TARGETS
            }
            populated_later = {k: v for k, v in later.items() if v}
            if populated_later:
                raise RuntimeError(
                    "Refusing Observation phase because later targets are populated: "
                    f"{populated_later}"
                )

            # Current Observation materializer is intentionally strict here.
            # These checks prevent partial mutation if a source would require
            # missing-date exclusion or if source lineage identifiers are not unique.
            date_issues = {
                "obs_gen_missing_start_date": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_OBS_GEN] "
                    "WHERE OBSGEN_START_DATE IS NULL",
                ),
                "lab_observation_missing_result_date": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN [{target_schema}].[concept] c
                      ON c.vocabulary_id='LOINC'
                     AND c.concept_code=LTRIM(RTRIM(CONVERT(nvarchar(255), l.LAB_LOINC)))
                     AND c.standard_concept='S'
                     AND c.invalid_reason IS NULL
                     AND c.domain_id='Observation'
                    WHERE l.RESULT_DATE IS NULL
                    """,
                ),
                "vital_categorical_missing_measure_date": _scalar(
                    con,
                    f"""
                    SELECT
                      COALESCE(SUM(
                        CASE WHEN MEASURE_DATE IS NULL AND SMOKING IS NOT NULL THEN 1 ELSE 0 END
                        + CASE WHEN MEASURE_DATE IS NULL AND TOBACCO IS NOT NULL THEN 1 ELSE 0 END
                        + CASE WHEN MEASURE_DATE IS NULL AND TOBACCO_TYPE IS NOT NULL THEN 1 ELSE 0 END
                      ), 0)
                    FROM [{source_schema}].[PCORnet_VITAL]
                    """,
                ),
            }
            if any(date_issues.values()):
                raise RuntimeError(
                    "Observation sources contain rows requiring explicit missing-date exclusion. "
                    "Refusing to materialize until the Observation transform applies that policy: "
                    f"{date_issues}"
                )

            duplicate_ids = {
                "OBSGENID": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT OBSGENID
                      FROM [{source_schema}].[PCORnet_OBS_GEN]
                      GROUP BY OBSGENID HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                ),
                "LAB_RESULT_CM_ID": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT LAB_RESULT_CM_ID
                      FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
                      GROUP BY LAB_RESULT_CM_ID HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                ),
                "VITALID": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                      SELECT VITALID
                      FROM [{source_schema}].[PCORnet_VITAL]
                      GROUP BY VITALID HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                ),
            }
            if any(duplicate_ids.values()):
                raise RuntimeError(
                    f"Observation source lineage IDs are not unique: {duplicate_ids}"
                )

            return {
                "status": "ready_for_phase6_observation",
                "measurement_rows": measurement_rows,
                "measurement_xwalk_rows": measurement_xwalk_rows,
                "obsclin_measurement_routes": obsclin_measurement_routes,
                "obsclin_measurement_xwalk": obsclin_measurement_xwalk,
                "observation_rows_before": observation_rows,
                "date_issues": date_issues,
                "duplicate_source_id_groups": duplicate_ids,
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
                "observation": _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{schema}].[observation]"
                ),
                "observation_xwalk": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_observation_xwalk]",
                ),
                "observation_overflow": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[etl_observation_text_overflow]",
                ),
            }
    finally:
        engine.dispose()


def run_clean_build_phase6_observation(config: EtlConfig) -> dict[str, object]:
    guard = _guard(config)
    result = transform_observation(config)
    counts = _post_counts(config)

    if counts["observation"] != counts["observation_xwalk"]:
        raise RuntimeError(f"Observation lineage mismatch: {counts}")
    if counts["observation"] != int(result.target_rows):
        raise RuntimeError(
            "Observation result/target mismatch: "
            f"result={result.target_rows:,}, actual={counts['observation']:,}"
        )
    if counts["observation_overflow"] != int(result.overflow_rows):
        raise RuntimeError(
            "Observation overflow result/table mismatch: "
            f"result={result.overflow_rows:,}, actual={counts['observation_overflow']:,}"
        )

    payload = {
        "stage": "clean_build_phase6_observation",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "phase6_observation_complete",
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "entry_guard": guard,
        "observation_result": {
            "status": result.status,
            "expected_rows": int(result.expected_rows),
            "target_rows": int(result.target_rows),
            "lineage_rows": int(result.lineage_rows),
            "obs_clin_rows": int(result.obs_clin_rows),
            "obs_gen_rows": int(result.obs_gen_rows),
            "lab_rows": int(result.lab_rows),
            "procedure_rows": int(result.procedure_rows),
            "vital_rows": int(result.vital_rows),
            "concept_zero_rows": int(result.concept_zero_rows),
            "vital_value_concept_zero_rows": int(result.vital_value_concept_zero_rows),
            "visit_linked_rows": int(result.visit_linked_rows),
            "overflow_rows": int(result.overflow_rows),
        },
        "post_counts": counts,
        "next_phase": (
            "Append OBS_CLIN Condition only after Observation source-family and lineage "
            "reconciliation are reviewed."
        ),
    }
    audit_path = config.audit_dir / "clean_build_phase6_observation.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run guarded clean-build Phase 6 Observation materialization."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase6_observation(load_etl_config(args.config))
    o = result["observation_result"]
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("entry_guard_status:", result["entry_guard"]["status"])
    print("expected_rows:", o["expected_rows"])
    print("target_rows:", o["target_rows"])
    print("lineage_rows:", o["lineage_rows"])
    print("obs_clin_rows:", o["obs_clin_rows"])
    print("obs_gen_rows:", o["obs_gen_rows"])
    print("lab_rows:", o["lab_rows"])
    print("procedure_rows:", o["procedure_rows"])
    print("vital_rows:", o["vital_rows"])
    print("concept_zero_rows:", o["concept_zero_rows"])
    print("vital_value_concept_zero_rows:", o["vital_value_concept_zero_rows"])
    print("visit_linked_rows:", o["visit_linked_rows"])
    print("overflow_rows:", o["overflow_rows"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
