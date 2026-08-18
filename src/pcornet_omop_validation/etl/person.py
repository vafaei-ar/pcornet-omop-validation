from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


@dataclass(frozen=True)
class PersonTransformResult:
    source_rows: int
    eligible_rows: int
    excluded_rows: int
    excluded_missing_patid: int
    excluded_missing_birth_date: int
    target_rows: int
    gender_concept_zero: int
    race_concept_zero: int
    ethnicity_concept_zero: int
    status: str
    audit_path: Path


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, schema: str) -> None:
    for table in ("PCORnet_DEMOGRAPHIC", "person"):
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def transform_person(config: EtlConfig) -> PersonTransformResult:
    """Transform staged PCORnet DEMOGRAPHIC records into OMOP person.

    Primary validated behavior:
    - PATID must be present and unique.
    - BIRTH_DATE is required because OMOP person.year_of_birth is NOT NULL.
      With policies.missing_required_date=exclude, missing birth dates are excluded
      rather than replaced by a sentinel date.
    - Recognized PCORnet demographic categories are mapped to OMOP concepts.
      Unrecognized/unknown categories use concept_id 0 while preserving source text.
    - IDs are deterministic within this clean target, ordered by PATID.
    """
    policy = config.raw.get("policies", {}).get("missing_required_date")
    if policy != "exclude":
        raise RuntimeError(
            "The validated person stage currently implements policies.missing_required_date=exclude only"
        )

    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    audit_path = config.audit_dir / "person_transform.json"
    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, schema)

            source_rows = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_DEMOGRAPHIC]")
            missing_patid = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_DEMOGRAPHIC] WHERE PATID IS NULL OR LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) = ''",
            )
            missing_birth = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_DEMOGRAPHIC] WHERE BIRTH_DATE IS NULL",
            )
            eligible_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_DEMOGRAPHIC] WHERE PATID IS NOT NULL AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> '' AND BIRTH_DATE IS NOT NULL",
            )
            excluded_rows = source_rows - eligible_rows

            duplicate_patids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT PATID
                    FROM [{schema}].[PCORnet_DEMOGRAPHIC]
                    WHERE PATID IS NOT NULL AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> ''
                    GROUP BY PATID
                    HAVING COUNT_BIG(*) > 1
                ) d
                """,
            )
            if duplicate_patids:
                raise RuntimeError(
                    f"PCORnet_DEMOGRAPHIC contains {duplicate_patids:,} duplicated PATID value(s); "
                    "person_id generation would be ambiguous"
                )

            existing = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[person]")
            if existing:
                if existing != eligible_rows:
                    raise RuntimeError(
                        f"Target [{schema}].[person] already contains {existing:,} rows but "
                        f"validated eligible source count is {eligible_rows:,}; refusing to overwrite"
                    )
                status = "already_loaded_matched"
            else:
                insert_sql = f"""
                WITH eligible AS (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY CAST(PATID AS nvarchar(255))) AS person_id,
                        PATID,
                        BIRTH_DATE,
                        SEX,
                        RACE,
                        HISPANIC
                    FROM [{schema}].[PCORnet_DEMOGRAPHIC]
                    WHERE PATID IS NOT NULL
                      AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> ''
                      AND BIRTH_DATE IS NOT NULL
                )
                INSERT INTO [{schema}].[person] (
                    person_id,
                    gender_concept_id,
                    year_of_birth,
                    month_of_birth,
                    day_of_birth,
                    birth_datetime,
                    race_concept_id,
                    ethnicity_concept_id,
                    person_source_value,
                    gender_source_value,
                    gender_source_concept_id,
                    race_source_value,
                    race_source_concept_id,
                    ethnicity_source_value,
                    ethnicity_source_concept_id
                )
                SELECT
                    person_id,
                    CASE
                        WHEN SEX = 'F' THEN 8532
                        WHEN SEX = 'M' THEN 8507
                        WHEN SEX = 'A' THEN 8570
                        WHEN SEX = 'OT' THEN 8521
                        ELSE 0
                    END,
                    YEAR(BIRTH_DATE),
                    MONTH(BIRTH_DATE),
                    DAY(BIRTH_DATE),
                    CAST(BIRTH_DATE AS datetime2(7)),
                    CASE
                        WHEN RACE = '01' THEN 8657
                        WHEN RACE = '02' THEN 8515
                        WHEN RACE = '03' THEN 8516
                        WHEN RACE = '04' THEN 8557
                        WHEN RACE = '05' THEN 8527
                        WHEN RACE = '06' THEN 8522
                        WHEN RACE = '07' THEN 8552
                        ELSE 0
                    END,
                    CASE
                        WHEN HISPANIC = 'Y' THEN 38003563
                        WHEN HISPANIC = 'N' THEN 38003564
                        ELSE 0
                    END,
                    CAST(PATID AS nvarchar(50)),
                    CAST(SEX AS nvarchar(50)),
                    0,
                    CAST(RACE AS nvarchar(50)),
                    0,
                    CAST(HISPANIC AS nvarchar(50)),
                    0
                FROM eligible
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "matched"

            target_rows = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[person]")
            if target_rows != eligible_rows:
                raise RuntimeError(
                    f"Person reconciliation failed: eligible_source={eligible_rows:,}, target={target_rows:,}"
                )

            distinct_source_values = _scalar(
                connection,
                f"SELECT COUNT_BIG(DISTINCT person_source_value) FROM [{schema}].[person]",
            )
            if distinct_source_values != target_rows:
                raise RuntimeError(
                    "person_source_value is not one-to-one with person_id after transformation"
                )

            gender_zero = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[person] WHERE gender_concept_id = 0")
            race_zero = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[person] WHERE race_concept_id = 0")
            ethnicity_zero = _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{schema}].[person] WHERE ethnicity_concept_id = 0")
    finally:
        engine.dispose()

    result = PersonTransformResult(
        source_rows=source_rows,
        eligible_rows=eligible_rows,
        excluded_rows=excluded_rows,
        excluded_missing_patid=missing_patid,
        excluded_missing_birth_date=missing_birth,
        target_rows=target_rows,
        gender_concept_zero=gender_zero,
        race_concept_zero=race_zero,
        ethnicity_concept_zero=ethnicity_zero,
        status=status,
        audit_path=audit_path,
    )

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "person",
                "source_table": f"{schema}.PCORnet_DEMOGRAPHIC",
                "target_table": f"{schema}.person",
                "policies": {
                    "missing_required_date": policy,
                    "unmapped_standard_concept": config.raw.get("policies", {}).get("unmapped_standard_concept"),
                },
                "mapping_strategy": {
                    "id": "deterministic ROW_NUMBER ordered by PATID in a clean target",
                    "unknown_demographic_concepts": "concept_id 0 with source values preserved",
                    "source_concept_ids": "0; source text retained explicitly",
                },
                "result": {**asdict(result), "audit_path": str(audit_path)},
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    return result
