from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig, load_etl_config
from .database import make_engine, table_exists
from .condition_occurrence import DX_SOURCE_STATUS_MAP


PERSON_RACE_MAP = {
    "01": 8657,
    "02": 8515,
    "03": 8516,
    "04": 8557,
    "05": 8527,
    "06": 8522,
    "07": 8552,
}


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _rows(con, sql: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
    return [dict(r) for r in con.execute(text(sql), params or {}).mappings().all()]


def run_diagnostic(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_DEMOGRAPHIC"),
                (source_schema, "PCORnet_DIAGNOSIS"),
                (target_schema, "person"),
                (target_schema, "condition_occurrence"),
                (target_schema, "concept"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            race_ids = sorted(set(PERSON_RACE_MAP.values()))
            status_ids = sorted(set(DX_SOURCE_STATUS_MAP.values()))
            all_ids = sorted(set(race_ids + status_ids))
            id_list = ",".join(str(x) for x in all_ids)

            concept_metadata = _rows(
                con,
                f"""
                SELECT concept_id, concept_name, domain_id, vocabulary_id, concept_code,
                       standard_concept, invalid_reason
                FROM {t('concept')}
                WHERE concept_id IN ({id_list})
                ORDER BY concept_id
                """,
            )

            invalid_person_race_rows = _rows(
                con,
                f"""
                SELECT p.race_concept_id AS concept_id, c.concept_name, c.domain_id,
                       c.vocabulary_id, c.concept_code, c.standard_concept, c.invalid_reason,
                       COUNT_BIG(*) AS rows
                FROM {t('person')} p
                LEFT JOIN {t('concept')} c ON c.concept_id=p.race_concept_id
                WHERE p.race_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Race'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                GROUP BY p.race_concept_id, c.concept_name, c.domain_id,
                         c.vocabulary_id, c.concept_code, c.standard_concept, c.invalid_reason
                ORDER BY rows DESC, concept_id
                """,
            )

            race_source_profile = _rows(
                con,
                f"""
                SELECT LTRIM(RTRIM(CONVERT(nvarchar(50), d.RACE))) AS source_race,
                       p.race_concept_id,
                       c.concept_name, c.domain_id, c.standard_concept, c.invalid_reason,
                       COUNT_BIG(*) AS rows
                FROM {s('PCORnet_DEMOGRAPHIC')} d
                JOIN {t('person')} p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(50), d.PATID)))
                LEFT JOIN {t('concept')} c ON c.concept_id=p.race_concept_id
                WHERE p.race_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Race'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(50), d.RACE))), p.race_concept_id,
                         c.concept_name, c.domain_id, c.standard_concept, c.invalid_reason
                ORDER BY rows DESC, source_race
                """,
            )

            invalid_condition_status_rows = _rows(
                con,
                f"""
                SELECT co.condition_status_concept_id AS concept_id, c.concept_name, c.domain_id,
                       c.vocabulary_id, c.concept_code, c.standard_concept, c.invalid_reason,
                       COUNT_BIG(*) AS rows
                FROM {t('condition_occurrence')} co
                LEFT JOIN {t('concept')} c ON c.concept_id=co.condition_status_concept_id
                WHERE co.condition_status_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Condition Status'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                GROUP BY co.condition_status_concept_id, c.concept_name, c.domain_id,
                         c.vocabulary_id, c.concept_code, c.standard_concept, c.invalid_reason
                ORDER BY rows DESC, concept_id
                """,
            )

            condition_status_source_profile = _rows(
                con,
                f"""
                SELECT LTRIM(RTRIM(CONVERT(nvarchar(50), d.DX_SOURCE))) AS dx_source,
                       co.condition_status_concept_id AS concept_id,
                       c.concept_name, c.domain_id, c.standard_concept, c.invalid_reason,
                       COUNT_BIG(*) AS rows
                FROM {t('etl_condition_occurrence_xwalk')} x
                JOIN {t('condition_occurrence')} co
                  ON co.condition_occurrence_id=x.condition_occurrence_id
                JOIN {s('PCORnet_DIAGNOSIS')} d
                  ON x.source_domain='DIAGNOSIS'
                 AND x.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255), d.DIAGNOSISID)))
                LEFT JOIN {t('concept')} c ON c.concept_id=co.condition_status_concept_id
                WHERE co.condition_status_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Condition Status'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(50), d.DX_SOURCE))),
                         co.condition_status_concept_id, c.concept_name, c.domain_id,
                         c.standard_concept, c.invalid_reason
                ORDER BY rows DESC, dx_source
                """,
            )

        payload = {
            "stage": "freeze_integrity_blocker_diagnostic",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "person_race_map": PERSON_RACE_MAP,
            "condition_status_map": DX_SOURCE_STATUS_MAP,
            "mapped_concept_metadata": concept_metadata,
            "invalid_person_race_concepts": invalid_person_race_rows,
            "invalid_person_race_source_profile": race_source_profile,
            "invalid_condition_status_concepts": invalid_condition_status_rows,
            "invalid_condition_status_source_profile": condition_status_source_profile,
            "status": "diagnostic_complete",
            "note": (
                "Read-only diagnostic. Do not patch the populated target. Use these results to "
                "correct generalized source-to-OMOP mapping rules, then perform a clean rebuild."
            ),
        }
        audit_path = config.audit_dir / "freeze_integrity_blocker_diagnostic.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        payload["audit_path"] = str(audit_path)
        return payload
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose auxiliary concept integrity blockers read-only.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    result = run_diagnostic(load_etl_config(args.config))
    print("status:", result["status"])
    print("invalid_person_race_concepts:", result["invalid_person_race_concepts"])
    print("invalid_person_race_source_profile:", result["invalid_person_race_source_profile"])
    print("invalid_condition_status_concepts:", result["invalid_condition_status_concepts"])
    print("invalid_condition_status_source_profile:", result["invalid_condition_status_source_profile"])
    print("mapped_concept_metadata:")
    for row in result["mapped_concept_metadata"]:
        print(" ", row)
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
