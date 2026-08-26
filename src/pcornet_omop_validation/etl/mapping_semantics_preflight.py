from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_occurrence import (
    DX_ORIGIN_TYPE_MAP,
    DX_SOURCE_STATUS_MAP,
    _validated_existing_map,
)
from .config import EtlConfig, load_etl_config
from .database import make_engine
from .person import PERSON_MAPPING_CONCEPTS, _validate_mapping_concepts


def run_mapping_semantics_preflight(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "mapping_semantics_preflight.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            person_validation = _validate_mapping_concepts(con, target_schema)
            type_valid, type_rejected = _validated_existing_map(
                con, target_schema, DX_ORIGIN_TYPE_MAP, "Type Concept"
            )
            status_valid, status_rejected = _validated_existing_map(
                con, target_schema, DX_SOURCE_STATUS_MAP, "Condition Status"
            )

            race_profile = [
                {"race": str(r[0]), "rows": int(r[1])}
                for r in con.execute(
                    text(
                        f"""
                        SELECT COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), RACE))), ''), '<NULL>'),
                               COUNT_BIG(*)
                        FROM [{source_schema}].[PCORnet_DEMOGRAPHIC]
                        GROUP BY COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), RACE))), ''), '<NULL>')
                        ORDER BY COUNT_BIG(*) DESC
                        """
                    )
                ).fetchall()
            ]
            dx_source_profile = [
                {"dx_source": str(r[0]), "rows": int(r[1])}
                for r in con.execute(
                    text(
                        f"""
                        SELECT COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DX_SOURCE))), ''), '<NULL>'),
                               COUNT_BIG(*)
                        FROM [{source_schema}].[PCORnet_DIAGNOSIS]
                        GROUP BY COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DX_SOURCE))), ''), '<NULL>')
                        ORDER BY COUNT_BIG(*) DESC
                        """
                    )
                ).fetchall()
            ]
            dx_origin_profile = [
                {"dx_origin": str(r[0]), "rows": int(r[1])}
                for r in con.execute(
                    text(
                        f"""
                        SELECT COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DX_ORIGIN))), ''), '<NULL>'),
                               COUNT_BIG(*)
                        FROM [{source_schema}].[PCORnet_DIAGNOSIS]
                        GROUP BY COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DX_ORIGIN))), ''), '<NULL>')
                        ORDER BY COUNT_BIG(*) DESC
                        """
                    )
                ).fetchall()
            ]
    finally:
        engine.dispose()

    payload = {
        "stage": "mapping_semantics_preflight",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "matched",
        "person_positive_mapping_concepts": PERSON_MAPPING_CONCEPTS,
        "person_mapping_validation": person_validation,
        "diagnosis_type_valid_map": type_valid,
        "diagnosis_type_rejected_map": type_rejected,
        "diagnosis_status_valid_map": status_valid,
        "diagnosis_status_rejected_map": status_rejected,
        "source_race_profile": race_profile,
        "source_dx_source_profile": dx_source_profile,
        "source_dx_origin_profile": dx_origin_profile,
        "policy": (
            "Emit nonzero demographic, Condition type, and Condition status concepts only when the configured concept is active, Standard, and in the exact expected OMOP domain. Otherwise retain the source value and use concept_id 0."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return {**payload, "audit_path": str(audit_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight for corrected auxiliary concept mappings.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    result = run_mapping_semantics_preflight(load_etl_config(args.config))
    p = result["person_mapping_validation"]
    print("status:", result["status"])
    print("person_gender_valid_map:", p["gender_valid_map"])
    print("person_gender_rejected_map:", p["gender_rejected_map"])
    print("person_race_valid_map:", p["race_valid_map"])
    print("person_race_rejected_map:", p["race_rejected_map"])
    print("person_ethnicity_valid_map:", p["ethnicity_valid_map"])
    print("person_ethnicity_rejected_map:", p["ethnicity_rejected_map"])
    print("diagnosis_type_valid_map:", result["diagnosis_type_valid_map"])
    print("diagnosis_type_rejected_map:", result["diagnosis_type_rejected_map"])
    print("diagnosis_status_valid_map:", result["diagnosis_status_valid_map"])
    print("diagnosis_status_rejected_map:", result["diagnosis_status_rejected_map"])
    print("source_race_profile:", result["source_race_profile"])
    print("source_dx_source_profile:", result["source_dx_source_profile"])
    print("source_dx_origin_profile:", result["source_dx_origin_profile"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
