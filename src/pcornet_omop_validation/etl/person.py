from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


# These are source-to-concept candidates, not unconditional target assignments.
# At runtime a candidate is emitted only when the loaded OMOP vocabulary confirms
# that it is active, Standard, and in the expected semantic domain. Otherwise the
# source category is preserved and the target concept is 0.
GENDER_SOURCE_MAP = {
    "F": 8532,
    "M": 8507,
    "A": 8570,
    "OT": 8521,
}
RACE_SOURCE_MAP = {
    "01": 8657,
    "02": 8515,
    "03": 8516,
    "04": 8557,
    "05": 8527,
    "06": 8522,
    "07": 8552,
}
ETHNICITY_SOURCE_MAP = {
    "Y": 38003563,
    "N": 38003564,
}

PERSON_MAPPING_CONCEPTS = {
    **{concept_id: "Gender" for concept_id in GENDER_SOURCE_MAP.values()},
    **{concept_id: "Race" for concept_id in RACE_SOURCE_MAP.values()},
    **{concept_id: "Ethnicity" for concept_id in ETHNICITY_SOURCE_MAP.values()},
}
PERSON_STANDARD_CONCEPTS = tuple(PERSON_MAPPING_CONCEPTS)


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


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_DEMOGRAPHIC"),
        (target_schema, "person"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _validate_mapping_concepts(connection, schema: str) -> dict[str, object]:
    """Classify Person mapping candidates against the loaded OMOP vocabulary.

    A missing/deprecated/nonstandard/wrong-domain candidate is rejected rather than
    treated as a fatal vocabulary error. The materializer then emits concept 0 for
    source categories whose candidate was rejected, preserving the source value.
    """
    values = ",".join(str(value) for value in sorted(PERSON_MAPPING_CONCEPTS))
    rows = connection.execute(
        text(
            f"SELECT concept_id, concept_name, domain_id, vocabulary_id, concept_code, "
            f"standard_concept, invalid_reason "
            f"FROM [{schema}].[concept] WHERE concept_id IN ({values})"
        )
    ).fetchall()
    observed = {
        int(row[0]): {
            "concept_name": row[1],
            "domain_id": row[2],
            "vocabulary_id": row[3],
            "concept_code": row[4],
            "standard_concept": row[5],
            "invalid_reason": row[6],
        }
        for row in rows
    }

    valid_ids: set[int] = set()
    rejected: dict[int, list[str]] = {}
    for concept_id, expected_domain in PERSON_MAPPING_CONCEPTS.items():
        row = observed.get(concept_id)
        reasons: list[str] = []
        if row is None:
            reasons.append("missing")
        else:
            if row["domain_id"] != expected_domain:
                reasons.append(
                    f"domain={row['domain_id']!r},expected={expected_domain!r}"
                )
            if row["standard_concept"] != "S":
                reasons.append(f"standard_concept={row['standard_concept']!r}")
            if row["invalid_reason"] is not None:
                reasons.append(f"invalid_reason={row['invalid_reason']!r}")
        if reasons:
            rejected[concept_id] = reasons
        else:
            valid_ids.add(concept_id)

    def classify(source_map: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        valid = {code: cid for code, cid in source_map.items() if cid in valid_ids}
        rejected_map = {code: cid for code, cid in source_map.items() if cid not in valid_ids}
        return valid, rejected_map

    gender_valid, gender_rejected = classify(GENDER_SOURCE_MAP)
    race_valid, race_rejected = classify(RACE_SOURCE_MAP)
    ethnicity_valid, ethnicity_rejected = classify(ETHNICITY_SOURCE_MAP)

    return {
        "metadata": observed,
        "rejected_concepts": rejected,
        "gender_valid_map": gender_valid,
        "gender_rejected_map": gender_rejected,
        "race_valid_map": race_valid,
        "race_rejected_map": race_rejected,
        "ethnicity_valid_map": ethnicity_valid,
        "ethnicity_rejected_map": ethnicity_rejected,
    }


def _case_sql(column: str, mapping: dict[str, int]) -> str:
    clauses = " ".join(
        f"WHEN {column} = '{key}' THEN {value}" for key, value in mapping.items()
    )
    return f"CASE {clauses} ELSE 0 END"


def transform_person(config: EtlConfig) -> PersonTransformResult:
    """Transform staged PCORnet DEMOGRAPHIC records into OMOP person.

    Validated behavior:
    - PATID must be present and unique.
    - BIRTH_DATE is required because OMOP person.year_of_birth is NOT NULL.
    - Demographic source categories are mapped only when their configured candidate
      is active, Standard, and in the exact expected OMOP domain in the loaded
      vocabulary.
    - Missing/deprecated/nonstandard/wrong-domain candidates become concept_id 0,
      with the original source category preserved.
    - person_id is deterministic within this clean target, ordered by PATID.
    """
    policies = config.raw.get("policies", {}) or {}
    date_policy = policies.get("missing_required_date")
    concept_policy = policies.get("unmapped_standard_concept")
    if date_policy != "exclude":
        raise RuntimeError(
            "The validated person stage currently implements policies.missing_required_date=exclude only"
        )
    if concept_policy != "concept_zero":
        raise RuntimeError(
            "The validated person stage currently implements policies.unmapped_standard_concept=concept_zero only"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "person_transform.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            mapping_validation = _validate_mapping_concepts(connection, target_schema)
            gender_map = dict(mapping_validation["gender_valid_map"])
            race_map = dict(mapping_validation["race_valid_map"])
            ethnicity_map = dict(mapping_validation["ethnicity_valid_map"])

            source_table = f"[{source_schema}].[PCORnet_DEMOGRAPHIC]"
            target_table = f"[{target_schema}].[person]"

            source_rows = _scalar(connection, f"SELECT COUNT_BIG(*) FROM {source_table}")
            missing_patid = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {source_table} "
                "WHERE PATID IS NULL OR LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) = ''",
            )
            missing_birth = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {source_table} WHERE BIRTH_DATE IS NULL",
            )
            eligible_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {source_table} "
                "WHERE PATID IS NOT NULL "
                "AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> '' "
                "AND BIRTH_DATE IS NOT NULL",
            )
            excluded_rows = source_rows - eligible_rows

            duplicate_patids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT PATID
                    FROM {source_table}
                    WHERE PATID IS NOT NULL
                      AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> ''
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

            existing = _scalar(connection, f"SELECT COUNT_BIG(*) FROM {target_table}")
            if existing:
                if existing != eligible_rows:
                    raise RuntimeError(
                        f"Target {target_table} already contains {existing:,} rows but "
                        f"validated eligible source count is {eligible_rows:,}; refusing to overwrite"
                    )
                invalid_existing = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {target_table} p
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = p.gender_concept_id
                    WHERE p.gender_concept_id <> 0
                      AND (c.concept_id IS NULL OR c.domain_id <> 'Gender' OR c.standard_concept <> 'S' OR c.invalid_reason IS NOT NULL)
                    UNION ALL
                    SELECT COUNT_BIG(*)
                    FROM {target_table} p
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = p.race_concept_id
                    WHERE p.race_concept_id <> 0
                      AND (c.concept_id IS NULL OR c.domain_id <> 'Race' OR c.standard_concept <> 'S' OR c.invalid_reason IS NOT NULL)
                    UNION ALL
                    SELECT COUNT_BIG(*)
                    FROM {target_table} p
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = p.ethnicity_concept_id
                    WHERE p.ethnicity_concept_id <> 0
                      AND (c.concept_id IS NULL OR c.domain_id <> 'Ethnicity' OR c.standard_concept <> 'S' OR c.invalid_reason IS NOT NULL)
                    """,
                )
                if invalid_existing:
                    raise RuntimeError(
                        "Existing Person materialization contains invalid nonzero demographic concepts; clean rebuild required"
                    )
                status = "already_loaded_matched"
            else:
                gender_case = _case_sql("SEX", gender_map)
                race_case = _case_sql("RACE", race_map)
                ethnicity_case = _case_sql("HISPANIC", ethnicity_map)
                insert_sql = f"""
                WITH eligible AS (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY CAST(PATID AS nvarchar(255))) AS person_id,
                        PATID,
                        BIRTH_DATE,
                        SEX,
                        RACE,
                        HISPANIC
                    FROM {source_table}
                    WHERE PATID IS NOT NULL
                      AND LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) <> ''
                      AND BIRTH_DATE IS NOT NULL
                )
                INSERT INTO {target_table} (
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
                    {gender_case},
                    YEAR(BIRTH_DATE),
                    MONTH(BIRTH_DATE),
                    DAY(BIRTH_DATE),
                    CAST(BIRTH_DATE AS datetime2(7)),
                    {race_case},
                    {ethnicity_case},
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

            target_rows = _scalar(connection, f"SELECT COUNT_BIG(*) FROM {target_table}")
            if target_rows != eligible_rows:
                raise RuntimeError(
                    f"Person reconciliation failed: eligible_source={eligible_rows:,}, target={target_rows:,}"
                )

            distinct_source_values = _scalar(
                connection,
                f"SELECT COUNT(DISTINCT person_source_value) FROM {target_table}",
            )
            if distinct_source_values != target_rows:
                raise RuntimeError(
                    "person_source_value is not one-to-one with person_id after transformation"
                )

            gender_zero = _scalar(
                connection, f"SELECT COUNT_BIG(*) FROM {target_table} WHERE gender_concept_id = 0"
            )
            race_zero = _scalar(
                connection, f"SELECT COUNT_BIG(*) FROM {target_table} WHERE race_concept_id = 0"
            )
            ethnicity_zero = _scalar(
                connection, f"SELECT COUNT_BIG(*) FROM {target_table} WHERE ethnicity_concept_id = 0"
            )
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
                "source_table": f"{source_schema}.PCORnet_DEMOGRAPHIC",
                "target_table": f"{target_schema}.person",
                "policies": {
                    "missing_required_date": date_policy,
                    "unmapped_standard_concept": concept_policy,
                },
                "mapping_strategy": {
                    "id": "deterministic ROW_NUMBER ordered by PATID in a clean target",
                    "unknown_demographic_concepts": "concept_id 0 with source values preserved",
                    "retired_or_nonstandard_demographic_concepts": "concept_id 0; candidate mappings are validated against the loaded vocabulary before SQL generation",
                    "source_concept_ids": "0; source text retained explicitly",
                    "mapping_validation": mapping_validation,
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
