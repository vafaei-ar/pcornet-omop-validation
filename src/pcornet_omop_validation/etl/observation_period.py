from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


# PCORnet ENR_BASIS -> OMOP observation period type.
# I: medical insurance coverage -> Period while enrolled in insurance
# D: outpatient prescription drug coverage -> also an insurance enrollment period
# G: geography -> Geography based period
# A: algorithmic -> Period inferred by algorithm
# E: encounter-based -> Period covering healthcare encounters
PERIOD_TYPE_BY_BASIS: dict[str, int] = {
    "I": 44814722,
    "D": 44814722,
    "G": 44814723,
    "A": 44814725,
    "E": 44814724,
}


@dataclass(frozen=True)
class ObservationPeriodTransformResult:
    source_rows: int
    eligible_rows: int
    excluded_rows: int
    excluded_missing_patid: int
    excluded_missing_start_date: int
    excluded_missing_end_date: int
    excluded_invalid_interval: int
    excluded_unlinked_person: int
    unknown_basis_rows: int
    overlapping_or_adjacent_pairs: int
    target_rows: int
    period_type_concept_zero: int
    status: str
    audit_path: Path


def _scalar(connection, sql: str, params: dict | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _require_tables(connection, schema: str) -> None:
    for table in ("PCORnet_ENROLLMENT", "person", "observation_period", "concept"):
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _basis_distribution(connection, schema: str) -> dict[str, int]:
    rows = connection.execute(
        text(
            f"""
            SELECT COALESCE(CAST(ENR_BASIS AS nvarchar(50)), '<NULL>') AS basis,
                   COUNT_BIG(*) AS n
            FROM [{schema}].[PCORnet_ENROLLMENT]
            GROUP BY COALESCE(CAST(ENR_BASIS AS nvarchar(50)), '<NULL>')
            ORDER BY basis
            """
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def _validate_period_type_concepts(connection, schema: str) -> dict[int, str]:
    expected = sorted(set(PERIOD_TYPE_BY_BASIS.values()))
    placeholders = ", ".join(str(value) for value in expected)
    rows = connection.execute(
        text(
            f"""
            SELECT concept_id, concept_name, concept_class_id, invalid_reason
            FROM [{schema}].[concept]
            WHERE concept_id IN ({placeholders})
            """
        )
    ).all()
    found = {int(row[0]): (str(row[1]), str(row[2]), row[3]) for row in rows}
    missing = [value for value in expected if value not in found]
    if missing:
        raise RuntimeError(
            "Required observation-period type concept(s) absent from loaded vocabulary: "
            + ", ".join(str(value) for value in missing)
        )

    invalid = [
        (concept_id, name, concept_class, invalid_reason)
        for concept_id, (name, concept_class, invalid_reason) in found.items()
        if invalid_reason is not None
    ]
    if invalid:
        details = "; ".join(
            f"{concept_id} {name!r} invalid_reason={invalid_reason!r}"
            for concept_id, name, _concept_class, invalid_reason in invalid
        )
        raise RuntimeError(f"Observation-period type concept validation failed: {details}")

    return {concept_id: name for concept_id, (name, _concept_class, _invalid) in found.items()}


def transform_observation_period(config: EtlConfig) -> ObservationPeriodTransformResult:
    """Transform PCORnet ENROLLMENT into OMOP observation_period.

    Validated behavior:
    - Missing required start/end dates are excluded, never replaced with sentinel dates.
    - Invalid intervals (start > end) are excluded and audited.
    - PATID is linked through person.person_source_value.
    - ENR_BASIS is translated to OMOP observation-period type concepts.
    - Unknown ENR_BASIS values use concept_id 0 under the configured unmapped policy.
    - Existing overlapping or immediately adjacent periods for a person cause a hard
      failure because OMOP observation periods should not overlap or be back-to-back;
      automatic merging would erase source-basis provenance and needs an explicit policy.
    - IDs are deterministic in this clean target, ordered by source enrollment keys.
    """
    policies = config.raw.get("policies", {})
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "The validated observation_period stage currently requires "
            "policies.missing_required_date=exclude"
        )
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "The validated observation_period stage currently requires "
            "policies.unmapped_standard_concept=concept_zero"
        )

    schema = str(config.raw["sqlserver"].get("target_schema", "dbo"))
    audit_path = config.audit_dir / "observation_period_transform.json"
    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, schema)
            concept_names = _validate_period_type_concepts(connection, schema)
            basis_distribution = _basis_distribution(connection, schema)

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_ENROLLMENT]",
            )
            missing_patid = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[PCORnet_ENROLLMENT]
                WHERE PATID IS NULL OR LTRIM(RTRIM(CAST(PATID AS nvarchar(max)))) = ''
                """,
            )
            missing_start = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_ENROLLMENT] WHERE ENR_START_DATE IS NULL",
            )
            missing_end = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[PCORnet_ENROLLMENT] WHERE ENR_END_DATE IS NULL",
            )
            invalid_interval = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[PCORnet_ENROLLMENT]
                WHERE ENR_START_DATE IS NOT NULL
                  AND ENR_END_DATE IS NOT NULL
                  AND CAST(ENR_START_DATE AS date) > CAST(ENR_END_DATE AS date)
                """,
            )
            duplicate_keys = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT PATID, ENR_START_DATE, ENR_BASIS
                    FROM [{schema}].[PCORnet_ENROLLMENT]
                    GROUP BY PATID, ENR_START_DATE, ENR_BASIS
                    HAVING COUNT_BIG(*) > 1
                ) d
                """,
            )
            if duplicate_keys:
                raise RuntimeError(
                    f"PCORnet_ENROLLMENT contains {duplicate_keys:,} duplicated composite key(s) "
                    "for PATID + ENR_START_DATE + ENR_BASIS"
                )

            unlinked_person = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[PCORnet_ENROLLMENT] e
                LEFT JOIN [{schema}].[person] p
                  ON CAST(e.PATID AS nvarchar(50)) = p.person_source_value
                WHERE e.PATID IS NOT NULL
                  AND LTRIM(RTRIM(CAST(e.PATID AS nvarchar(max)))) <> ''
                  AND p.person_id IS NULL
                """,
            )
            unknown_basis = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[PCORnet_ENROLLMENT]
                WHERE ENR_BASIS IS NULL
                   OR CAST(ENR_BASIS AS nvarchar(50)) NOT IN ('I','D','G','A','E')
                """,
            )

            eligible_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{schema}].[PCORnet_ENROLLMENT] e
                JOIN [{schema}].[person] p
                  ON CAST(e.PATID AS nvarchar(50)) = p.person_source_value
                WHERE e.PATID IS NOT NULL
                  AND LTRIM(RTRIM(CAST(e.PATID AS nvarchar(max)))) <> ''
                  AND e.ENR_START_DATE IS NOT NULL
                  AND e.ENR_END_DATE IS NOT NULL
                  AND CAST(e.ENR_START_DATE AS date) <= CAST(e.ENR_END_DATE AS date)
                """,
            )
            excluded_rows = source_rows - eligible_rows

            overlap_pairs = _scalar(
                connection,
                f"""
                WITH periods AS (
                    SELECT
                        p.person_id,
                        CAST(e.ENR_START_DATE AS date) AS start_date,
                        CAST(e.ENR_END_DATE AS date) AS end_date,
                        LAG(CAST(e.ENR_END_DATE AS date)) OVER (
                            PARTITION BY p.person_id
                            ORDER BY CAST(e.ENR_START_DATE AS date),
                                     CAST(e.ENR_END_DATE AS date),
                                     CAST(e.ENR_BASIS AS nvarchar(50))
                        ) AS previous_end
                    FROM [{schema}].[PCORnet_ENROLLMENT] e
                    JOIN [{schema}].[person] p
                      ON CAST(e.PATID AS nvarchar(50)) = p.person_source_value
                    WHERE e.ENR_START_DATE IS NOT NULL
                      AND e.ENR_END_DATE IS NOT NULL
                      AND CAST(e.ENR_START_DATE AS date) <= CAST(e.ENR_END_DATE AS date)
                )
                SELECT COUNT_BIG(*)
                FROM periods
                WHERE previous_end IS NOT NULL
                  AND start_date <= DATEADD(day, 1, previous_end)
                """,
            )
            if overlap_pairs:
                raise RuntimeError(
                    f"Detected {overlap_pairs:,} overlapping or immediately adjacent enrollment period pair(s). "
                    "OMOP observation periods should not overlap or be back-to-back. The validated ETL will not "
                    "merge them silently because that could erase ENR_BASIS provenance."
                )

            existing = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[observation_period]",
            )
            if existing:
                if existing != eligible_rows:
                    raise RuntimeError(
                        f"Target [{schema}].[observation_period] already contains {existing:,} rows but "
                        f"validated eligible source count is {eligible_rows:,}; refusing to overwrite"
                    )
                status = "already_loaded_matched"
            else:
                insert_sql = f"""
                WITH eligible AS (
                    SELECT
                        ROW_NUMBER() OVER (
                            ORDER BY CAST(e.PATID AS nvarchar(255)),
                                     CAST(e.ENR_START_DATE AS date),
                                     CAST(e.ENR_END_DATE AS date),
                                     CAST(e.ENR_BASIS AS nvarchar(50))
                        ) AS observation_period_id,
                        p.person_id,
                        CAST(e.ENR_START_DATE AS date) AS start_date,
                        CAST(e.ENR_END_DATE AS date) AS end_date,
                        CASE CAST(e.ENR_BASIS AS nvarchar(50))
                            WHEN 'I' THEN 44814722
                            WHEN 'D' THEN 44814722
                            WHEN 'G' THEN 44814723
                            WHEN 'A' THEN 44814725
                            WHEN 'E' THEN 44814724
                            ELSE 0
                        END AS period_type_concept_id
                    FROM [{schema}].[PCORnet_ENROLLMENT] e
                    JOIN [{schema}].[person] p
                      ON CAST(e.PATID AS nvarchar(50)) = p.person_source_value
                    WHERE e.PATID IS NOT NULL
                      AND LTRIM(RTRIM(CAST(e.PATID AS nvarchar(max)))) <> ''
                      AND e.ENR_START_DATE IS NOT NULL
                      AND e.ENR_END_DATE IS NOT NULL
                      AND CAST(e.ENR_START_DATE AS date) <= CAST(e.ENR_END_DATE AS date)
                )
                INSERT INTO [{schema}].[observation_period] (
                    observation_period_id,
                    person_id,
                    observation_period_start_date,
                    observation_period_end_date,
                    period_type_concept_id
                )
                SELECT
                    observation_period_id,
                    person_id,
                    start_date,
                    end_date,
                    period_type_concept_id
                FROM eligible
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[observation_period]",
            )
            if target_rows != eligible_rows:
                raise RuntimeError(
                    "Observation-period reconciliation failed: "
                    f"eligible_source={eligible_rows:,}, target={target_rows:,}"
                )
            concept_zero = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{schema}].[observation_period] WHERE period_type_concept_id = 0",
            )
    finally:
        engine.dispose()

    result = ObservationPeriodTransformResult(
        source_rows=source_rows,
        eligible_rows=eligible_rows,
        excluded_rows=excluded_rows,
        excluded_missing_patid=missing_patid,
        excluded_missing_start_date=missing_start,
        excluded_missing_end_date=missing_end,
        excluded_invalid_interval=invalid_interval,
        excluded_unlinked_person=unlinked_person,
        unknown_basis_rows=unknown_basis,
        overlapping_or_adjacent_pairs=overlap_pairs,
        target_rows=target_rows,
        period_type_concept_zero=concept_zero,
        status=status,
        audit_path=audit_path,
    )

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "observation_period",
                "source_table": f"{schema}.PCORnet_ENROLLMENT",
                "target_table": f"{schema}.observation_period",
                "policies": {
                    "missing_required_date": policies.get("missing_required_date"),
                    "unmapped_standard_concept": policies.get("unmapped_standard_concept"),
                },
                "source_basis_distribution": basis_distribution,
                "basis_mapping": {
                    "I": {"concept_id": 44814722, "concept_name": concept_names.get(44814722)},
                    "D": {
                        "concept_id": 44814722,
                        "concept_name": concept_names.get(44814722),
                        "rationale": "Outpatient prescription drug coverage is an insurance enrollment period; OMOP has no separate drug-coverage observation-period type in this mapping set.",
                    },
                    "G": {"concept_id": 44814723, "concept_name": concept_names.get(44814723)},
                    "A": {"concept_id": 44814725, "concept_name": concept_names.get(44814725)},
                    "E": {"concept_id": 44814724, "concept_name": concept_names.get(44814724)},
                    "other": {"concept_id": 0, "rationale": "Unmapped source basis retained as concept_id 0 under configured policy."},
                },
                "historical_reference_note": (
                    "The local historical converter used several generic Type concepts for ENR_BASIS values. "
                    "This validated implementation instead uses OMOP observation-period-specific concepts "
                    "aligned to the PCORnet ENR_BASIS semantics and records the mapping explicitly."
                ),
                "result": {**asdict(result), "audit_path": str(audit_path)},
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    return result
