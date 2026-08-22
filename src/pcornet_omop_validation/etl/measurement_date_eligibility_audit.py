from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one())


def audit_measurement_date_eligibility(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "measurement_date_eligibility_audit.json"

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (source_schema, "PCORnet_VITAL"),
                (target_schema, "etl_procedure_event_route"),
                (target_schema, "person"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            lab_source_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_LAB_RESULT_CM')}")
            lab_missing_result_date = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {s('PCORnet_LAB_RESULT_CM')} WHERE RESULT_DATE IS NULL",
            )
            lab_missing_id = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {s('PCORnet_LAB_RESULT_CM')}
                WHERE LAB_RESULT_CM_ID IS NULL
                   OR LTRIM(RTRIM(CONVERT(nvarchar(255), LAB_RESULT_CM_ID))) = ''
                """,
            )
            lab_duplicate_id_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM (
                    SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), LAB_RESULT_CM_ID))) AS source_id
                    FROM {s('PCORnet_LAB_RESULT_CM')}
                    WHERE LAB_RESULT_CM_ID IS NOT NULL
                    GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), LAB_RESULT_CM_ID)))
                    HAVING COUNT_BIG(*) > 1
                ) d
                """,
            )
            lab_unlinked_person = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {s('PCORnet_LAB_RESULT_CM')} l
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), l.PATID)))
                WHERE p.person_id IS NULL
                """,
            )

            vital_source_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {s('PCORnet_VITAL')}")
            vital_expanded_rows = _scalar(
                con,
                f"""
                SELECT COALESCE(SUM(
                    CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                ), 0)
                FROM {s('PCORnet_VITAL')}
                """,
            )
            vital_expanded_missing_date = _scalar(
                con,
                f"""
                SELECT COALESCE(SUM(
                    CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                ), 0)
                FROM {s('PCORnet_VITAL')}
                WHERE MEASURE_DATE IS NULL
                """,
            )
            vital_missing_id_with_measurement = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {s('PCORnet_VITAL')}
                WHERE (HT IS NOT NULL OR WT IS NOT NULL OR SYSTOLIC IS NOT NULL
                       OR DIASTOLIC IS NOT NULL OR ORIGINAL_BMI IS NOT NULL)
                  AND (
                    VITALID IS NULL
                    OR LTRIM(RTRIM(CONVERT(nvarchar(255), VITALID))) = ''
                  )
                """,
            )
            vital_duplicate_id_groups = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM (
                    SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), VITALID))) AS source_id
                    FROM {s('PCORnet_VITAL')}
                    WHERE VITALID IS NOT NULL
                    GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), VITALID)))
                    HAVING COUNT_BIG(*) > 1
                ) d
                """,
            )
            vital_unlinked_measurement_rows = _scalar(
                con,
                f"""
                SELECT COALESCE(SUM(
                    CASE WHEN v.HT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN v.WT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN v.SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN v.DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN v.ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                ), 0)
                FROM {s('PCORnet_VITAL')} v
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), v.PATID)))
                WHERE p.person_id IS NULL
                """,
            )

            procedure_measurement_rows = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('etl_procedure_event_route')} WHERE target_domain = 'Measurement'",
            )
            procedure_measurement_missing_date = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('etl_procedure_event_route')}
                WHERE target_domain = 'Measurement' AND px_date IS NULL
                """,
            )
            procedure_measurement_unlinked_person = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('etl_procedure_event_route')} r
                LEFT JOIN {t('person')} p ON p.person_source_value = r.patid
                WHERE r.target_domain = 'Measurement' AND p.person_id IS NULL
                """,
            )

        checks = {
            "lab_source_keys_unique_complete": lab_missing_id == 0 and lab_duplicate_id_groups == 0,
            "lab_person_linkage_complete": lab_unlinked_person == 0,
            "vital_source_keys_unique_complete": vital_missing_id_with_measurement == 0 and vital_duplicate_id_groups == 0,
            "vital_person_linkage_complete": vital_unlinked_measurement_rows == 0,
            "procedure_measurement_date_complete": procedure_measurement_missing_date == 0,
            "procedure_measurement_person_linkage_complete": procedure_measurement_unlinked_person == 0,
        }
        status = "matched" if all(checks.values()) else "review_required"
        payload = {
            "stage": "measurement_date_eligibility_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "lab_source_rows": lab_source_rows,
            "lab_missing_result_date": lab_missing_result_date,
            "lab_missing_id": lab_missing_id,
            "lab_duplicate_id_groups": lab_duplicate_id_groups,
            "lab_unlinked_person": lab_unlinked_person,
            "vital_source_rows": vital_source_rows,
            "vital_expanded_rows": vital_expanded_rows,
            "vital_expanded_missing_measure_date": vital_expanded_missing_date,
            "vital_missing_id_with_measurement": vital_missing_id_with_measurement,
            "vital_duplicate_id_groups": vital_duplicate_id_groups,
            "vital_unlinked_measurement_rows": vital_unlinked_measurement_rows,
            "procedure_measurement_rows": procedure_measurement_rows,
            "procedure_measurement_missing_px_date": procedure_measurement_missing_date,
            "procedure_measurement_unlinked_person": procedure_measurement_unlinked_person,
            "checks": checks,
            "status": status,
            "policy": (
                "Measurement rows require a source event date. Missing LAB RESULT_DATE or VITAL "
                "MEASURE_DATE rows are excluded and quantified; procedure Measurement routes are "
                "expected to have already enforced PX_DATE eligibility. Source identifiers and person "
                "linkage must be structurally valid for deterministic lineage."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    result = audit_measurement_date_eligibility(load_etl_config(args.config))
    for key in (
        "status",
        "lab_source_rows",
        "lab_missing_result_date",
        "lab_missing_id",
        "lab_duplicate_id_groups",
        "lab_unlinked_person",
        "vital_source_rows",
        "vital_expanded_rows",
        "vital_expanded_missing_measure_date",
        "vital_missing_id_with_measurement",
        "vital_duplicate_id_groups",
        "vital_unlinked_measurement_rows",
        "procedure_measurement_rows",
        "procedure_measurement_missing_px_date",
        "procedure_measurement_unlinked_person",
    ):
        print(f"{key}: {result[key]}")
    print("checks:")
    for key, value in result["checks"].items():
        print(f"  {key}: {value}")
    print(f"Audit: {result['audit_path']}")


if __name__ == "__main__":
    main()
