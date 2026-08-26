from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


DX_ORIGIN_TYPE_MAP = {
    "OD": 32817,
    "BI": 32821,
    "CL": 32810,
    "DR": 45754907,
    "NI": 44814650,
    "UN": 44814653,
    "OT": 44814649,
}

DX_SOURCE_STATUS_MAP = {
    "AD": 32890,
    "DI": 32896,
    "FI": 40492206,
    "IN": 40492208,
    "NI": 44814650,
    "UN": 44814653,
    "OT": 44814649,
}

CANONICAL_ROUTE_TABLE = "etl_condition_event_route_v2"
XWALK_TABLE = "etl_condition_occurrence_xwalk"


@dataclass(frozen=True)
class ConditionOccurrenceTransformResult:
    diagnosis_source_rows: int
    diagnosis_eligible_rows: int
    diagnosis_excluded_rows: int
    diagnosis_missing_id: int
    diagnosis_missing_patid: int
    diagnosis_unlinked_person: int
    diagnosis_missing_dx_date: int
    condition_source_rows: int
    condition_eligible_rows: int
    condition_excluded_rows: int
    condition_missing_id: int
    condition_missing_patid: int
    condition_unlinked_person: int
    condition_missing_date: int
    condition_report_date_fallback: int
    condition_invalid_interval: int
    target_rows: int
    diagnosis_target_rows: int
    condition_target_rows: int
    diagnosis_concept_zero: int
    condition_concept_zero: int
    diagnosis_source_concept_zero: int
    condition_source_concept_zero: int
    diagnosis_visit_linked: int
    condition_visit_linked: int
    lineage_rows: int
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
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_CONDITION"),
        (target_schema, "person"),
        (target_schema, "condition_occurrence"),
        (target_schema, "concept"),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, CANONICAL_ROUTE_TABLE),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _validated_existing_map(
    connection,
    schema: str,
    mapping: dict[str, int],
    expected_domain: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """Keep only active Standard concepts in the expected OMOP domain.

    A configured identifier being present in CONCEPT is not sufficient. Deprecated,
    non-Standard, or wrong-domain concepts are rejected to concept 0 by the caller.
    """
    ids = sorted(set(mapping.values()))
    if not ids:
        return {}, mapping
    values = ",".join(str(value) for value in ids)
    valid_ids = {
        int(row[0])
        for row in connection.execute(
            text(
                f"SELECT concept_id FROM [{schema}].[concept] "
                f"WHERE concept_id IN ({values}) "
                "AND domain_id = :expected_domain "
                "AND standard_concept = 'S' "
                "AND invalid_reason IS NULL"
            ),
            {"expected_domain": expected_domain},
        ).fetchall()
    }
    valid = {key: value for key, value in mapping.items() if value in valid_ids}
    rejected = {key: value for key, value in mapping.items() if value not in valid_ids}
    return valid, rejected


def _case_sql(column: str, mapping: dict[str, int]) -> str:
    clauses = " ".join(
        f"WHEN {column} = '{key}' THEN {value}" for key, value in mapping.items()
    )
    return f"CASE {clauses} ELSE 0 END"


def _vocabulary_case(column: str) -> str:
    return f"""
    CASE UPPER(LTRIM(RTRIM(CAST({column} AS nvarchar(50)))))
      WHEN '09' THEN 'ICD9CM'
      WHEN '9' THEN 'ICD9CM'
      WHEN 'ICD9' THEN 'ICD9CM'
      WHEN 'ICD9CM' THEN 'ICD9CM'
      WHEN '10' THEN 'ICD10CM'
      WHEN 'ICD10' THEN 'ICD10CM'
      WHEN 'ICD10CM' THEN 'ICD10CM'
      WHEN 'SM' THEN 'SNOMED'
      WHEN 'SNOMED' THEN 'SNOMED'
      WHEN 'SNOMEDCT' THEN 'SNOMED'
      ELSE NULL
    END
    """.strip()


def _eligible_ctes(source_schema: str, target_schema: str) -> str:
    """Return source-semantic eligibility CTEs only.

    Vocabulary mapping is intentionally not performed here. Canonical source-concept
    resolution and one-to-many Standard routing are owned by
    condition_canonical_routes.py so that no arbitrary TOP(1) mapping can enter the
    primary Condition materialization.
    """
    dx_vocab = _vocabulary_case("DX_TYPE")
    cond_vocab = _vocabulary_case("CONDITION_TYPE")
    return f"""
    WITH diag_eligible AS (
      SELECT d.*, p.person_id, v.visit_occurrence_id,
             {dx_vocab} AS vocabulary_id
      FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
      JOIN [{target_schema}].[person] p
        ON CAST(d.PATID AS nvarchar(50)) = p.person_source_value
      LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
        ON CAST(d.ENCOUNTERID AS nvarchar(255)) = v.encounterid
      WHERE d.DIAGNOSISID IS NOT NULL
        AND LTRIM(RTRIM(CAST(d.DIAGNOSISID AS nvarchar(max)))) <> ''
        AND d.DX_DATE IS NOT NULL
    ),
    cond_eligible AS (
      SELECT c.*, p.person_id, v.visit_occurrence_id,
             {cond_vocab} AS vocabulary_id,
             COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS effective_start_date
      FROM [{source_schema}].[PCORnet_CONDITION] c
      JOIN [{target_schema}].[person] p
        ON CAST(c.PATID AS nvarchar(50)) = p.person_source_value
      LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
        ON CAST(c.ENCOUNTERID AS nvarchar(255)) = v.encounterid
      WHERE c.CONDITIONID IS NOT NULL
        AND LTRIM(RTRIM(CAST(c.CONDITIONID AS nvarchar(max)))) <> ''
        AND COALESCE(c.ONSET_DATE, c.REPORT_DATE) IS NOT NULL
        AND (
          c.RESOLVE_DATE IS NULL
          OR CAST(c.RESOLVE_DATE AS date) >=
             CAST(COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS date)
        )
    )
    """


def _source_classification_counts(connection, source_schema: str, target_schema: str):
    diag_cte = f"""
    WITH classified AS (
      SELECT d.*,
             CASE
               WHEN d.DIAGNOSISID IS NULL
                 OR LTRIM(RTRIM(CAST(d.DIAGNOSISID AS nvarchar(max)))) = ''
                 THEN 'missing_id'
               WHEN d.PATID IS NULL
                 OR LTRIM(RTRIM(CAST(d.PATID AS nvarchar(max)))) = ''
                 THEN 'missing_patid'
               WHEN p.person_id IS NULL THEN 'unlinked_person'
               WHEN d.DX_DATE IS NULL THEN 'missing_dx_date'
               ELSE 'eligible'
             END AS record_status
      FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
      LEFT JOIN [{target_schema}].[person] p
        ON CAST(d.PATID AS nvarchar(50)) = p.person_source_value
    )
    """
    diag_counts = dict(
        connection.execute(
            text(
                diag_cte
                + " SELECT record_status, COUNT_BIG(*) FROM classified GROUP BY record_status"
            )
        ).fetchall()
    )

    cond_cte = f"""
    WITH classified AS (
      SELECT c.*,
             CASE
               WHEN c.CONDITIONID IS NULL
                 OR LTRIM(RTRIM(CAST(c.CONDITIONID AS nvarchar(max)))) = ''
                 THEN 'missing_id'
               WHEN c.PATID IS NULL
                 OR LTRIM(RTRIM(CAST(c.PATID AS nvarchar(max)))) = ''
                 THEN 'missing_patid'
               WHEN p.person_id IS NULL THEN 'unlinked_person'
               WHEN c.ONSET_DATE IS NULL AND c.REPORT_DATE IS NULL
                 THEN 'missing_date'
               WHEN c.RESOLVE_DATE IS NOT NULL
                AND CAST(c.RESOLVE_DATE AS date) <
                    CAST(COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS date)
                 THEN 'invalid_interval'
               ELSE 'eligible'
             END AS record_status
      FROM [{source_schema}].[PCORnet_CONDITION] c
      LEFT JOIN [{target_schema}].[person] p
        ON CAST(c.PATID AS nvarchar(50)) = p.person_source_value
    )
    """
    cond_counts = dict(
        connection.execute(
            text(
                cond_cte
                + " SELECT record_status, COUNT_BIG(*) FROM classified GROUP BY record_status"
            )
        ).fetchall()
    )
    report_fallback = _scalar(
        connection,
        cond_cte
        + " SELECT COUNT_BIG(*) FROM classified WHERE record_status='eligible' "
        "AND ONSET_DATE IS NULL AND REPORT_DATE IS NOT NULL",
    )
    return diag_counts, cond_counts, report_fallback


def transform_condition_occurrence(
    config: EtlConfig,
) -> ConditionOccurrenceTransformResult:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "The validated condition stage requires policies.missing_required_date=exclude"
        )
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "The validated condition stage requires policies.unmapped_standard_concept=concept_zero"
        )
    if policies.get("condition_sources") != "include_both":
        raise RuntimeError(
            "The validated condition stage requires policies.condition_sources=include_both"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "condition_occurrence_transform.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            diagnosis_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_DIAGNOSIS]",
            )
            condition_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_CONDITION]",
            )
            diag_counts, cond_counts, condition_report_date_fallback = (
                _source_classification_counts(connection, source_schema, target_schema)
            )

            diagnosis_eligible_rows = int(diag_counts.get("eligible", 0))
            diagnosis_missing_id = int(diag_counts.get("missing_id", 0))
            diagnosis_missing_patid = int(diag_counts.get("missing_patid", 0))
            diagnosis_unlinked_person = int(diag_counts.get("unlinked_person", 0))
            diagnosis_missing_dx_date = int(diag_counts.get("missing_dx_date", 0))
            diagnosis_excluded_rows = diagnosis_source_rows - diagnosis_eligible_rows

            condition_eligible_rows = int(cond_counts.get("eligible", 0))
            condition_missing_id = int(cond_counts.get("missing_id", 0))
            condition_missing_patid = int(cond_counts.get("missing_patid", 0))
            condition_unlinked_person = int(cond_counts.get("unlinked_person", 0))
            condition_missing_date = int(cond_counts.get("missing_date", 0))
            condition_invalid_interval = int(cond_counts.get("invalid_interval", 0))
            condition_excluded_rows = condition_source_rows - condition_eligible_rows

            duplicate_diag_ids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT DIAGNOSISID
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS]
                  WHERE DIAGNOSISID IS NOT NULL
                    AND LTRIM(RTRIM(CAST(DIAGNOSISID AS nvarchar(max)))) <> ''
                  GROUP BY DIAGNOSISID
                  HAVING COUNT_BIG(*) > 1
                ) x
                """,
            )
            duplicate_condition_ids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT CONDITIONID
                  FROM [{source_schema}].[PCORnet_CONDITION]
                  WHERE CONDITIONID IS NOT NULL
                    AND LTRIM(RTRIM(CAST(CONDITIONID AS nvarchar(max)))) <> ''
                  GROUP BY CONDITIONID
                  HAVING COUNT_BIG(*) > 1
                ) x
                """,
            )
            if duplicate_diag_ids or duplicate_condition_ids:
                raise RuntimeError(
                    "Source condition lineage IDs are not unique: "
                    f"DIAGNOSISID duplicates={duplicate_diag_ids:,}, "
                    f"CONDITIONID duplicates={duplicate_condition_ids:,}"
                )

            route_source_events = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT source_domain, source_record_id
                  FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}]
                  GROUP BY source_domain, source_record_id
                ) x
                """,
            )
            eligible_source_events = diagnosis_eligible_rows + condition_eligible_rows
            if route_source_events != eligible_source_events:
                raise RuntimeError(
                    "Canonical Condition route coverage does not match eligible source events: "
                    f"routes={route_source_events:,}, eligible={eligible_source_events:,}. "
                    "Rebuild the canonical route ledger before materializing Condition rows."
                )

            invalid_condition_targets = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE r.is_core_event_route = 1
                  AND r.target_domain = 'Condition'
                  AND r.target_concept_id <> 0
                  AND NOT (
                    c.concept_id IS NOT NULL
                    AND c.domain_id = 'Condition'
                    AND c.standard_concept = 'S'
                    AND c.invalid_reason IS NULL
                  )
                """,
            )
            if invalid_condition_targets:
                raise RuntimeError(
                    "Canonical route ledger contains invalid nonzero Condition targets: "
                    f"{invalid_condition_targets:,}"
                )

            expected_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}]
                WHERE is_core_event_route = 1 AND target_domain = 'Condition'
                """,
            )
            expected_diag_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}]
                WHERE is_core_event_route = 1 AND target_domain = 'Condition'
                  AND source_domain = 'DIAGNOSIS'
                """,
            )
            expected_cond_rows = expected_rows - expected_diag_rows

            type_map, rejected_type_map = _validated_existing_map(
                connection, target_schema, DX_ORIGIN_TYPE_MAP, "Type Concept"
            )
            status_map, rejected_status_map = _validated_existing_map(
                connection, target_schema, DX_SOURCE_STATUS_MAP, "Condition Status"
            )

            existing = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
            )
            if existing:
                if existing != expected_rows:
                    raise RuntimeError(
                        f"Target [{target_schema}].[condition_occurrence] already has "
                        f"{existing:,} rows; canonical Condition routes require {expected_rows:,}. "
                        "Refusing to patch or overwrite an existing materialization."
                    )
                if not table_exists(connection, target_schema, XWALK_TABLE):
                    raise RuntimeError("Condition target exists but route-aware lineage is missing")
                lineage_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]",
                )
                if lineage_rows != existing:
                    raise RuntimeError(
                        "Condition route-aware lineage does not match target: "
                        f"lineage={lineage_rows:,}, target={existing:,}"
                    )
                legacy_shape = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM sys.indexes i
                    JOIN sys.index_columns ic
                      ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                    JOIN sys.columns c
                      ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                    WHERE i.object_id = OBJECT_ID('[{target_schema}].[{XWALK_TABLE}]')
                      AND i.is_primary_key = 1
                      AND c.name = 'route_id'
                    """,
                )
                if legacy_shape == 0:
                    raise RuntimeError(
                        "Existing Condition lineage uses the legacy one-row-per-source schema. "
                        "A clean rebuild is required for route-aware one-to-many lineage."
                    )
                status = "already_loaded_matched"
            else:
                if table_exists(connection, target_schema, XWALK_TABLE):
                    raise RuntimeError(
                        f"[{target_schema}].[{XWALK_TABLE}] exists while condition_occurrence "
                        "is empty; refusing partial-state load"
                    )

                type_case = _case_sql("d.DX_ORIGIN", type_map)
                status_case = _case_sql("d.DX_SOURCE", status_map)
                eligible = _eligible_ctes(source_schema, target_schema)
                insert_sql = eligible + f"""
                , condition_routes AS (
                  SELECT *
                  FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}]
                  WHERE is_core_event_route = 1
                    AND target_domain = 'Condition'
                ),
                combined AS (
                  SELECT
                    r.route_id,
                    r.source_domain,
                    r.source_record_id,
                    d.person_id,
                    r.target_concept_id AS condition_concept_id,
                    CAST(d.DX_DATE AS date) AS condition_start_date,
                    CAST(d.DX_DATE AS datetime2(7)) AS condition_start_datetime,
                    CAST(NULL AS date) AS condition_end_date,
                    CAST(NULL AS datetime2(7)) AS condition_end_datetime,
                    {type_case} AS condition_type_concept_id,
                    {status_case} AS condition_status_concept_id,
                    CAST(NULL AS bigint) AS provider_id,
                    d.visit_occurrence_id,
                    CAST(d.DX AS nvarchar(255)) AS condition_source_value,
                    r.source_concept_id AS condition_source_concept_id,
                    CAST(d.DX_SOURCE AS nvarchar(50)) AS condition_status_source_value,
                    r.source_code_type,
                    r.source_provenance,
                    r.date_basis,
                    r.route_status,
                    r.is_fallback
                  FROM condition_routes r
                  JOIN diag_eligible d
                    ON r.source_domain = 'DIAGNOSIS'
                   AND r.source_record_id = CAST(d.DIAGNOSISID AS nvarchar(255))

                  UNION ALL

                  SELECT
                    r.route_id,
                    r.source_domain,
                    r.source_record_id,
                    c.person_id,
                    r.target_concept_id,
                    CAST(c.effective_start_date AS date),
                    CAST(c.effective_start_date AS datetime2(7)),
                    CAST(c.RESOLVE_DATE AS date),
                    CAST(c.RESOLVE_DATE AS datetime2(7)),
                    0,
                    0,
                    CAST(NULL AS bigint),
                    c.visit_occurrence_id,
                    CAST(c.CONDITION AS nvarchar(255)),
                    r.source_concept_id,
                    CAST(c.CONDITION_STATUS AS nvarchar(50)),
                    r.source_code_type,
                    r.source_provenance,
                    r.date_basis,
                    r.route_status,
                    r.is_fallback
                  FROM condition_routes r
                  JOIN cond_eligible c
                    ON r.source_domain = 'CONDITION'
                   AND r.source_record_id = CAST(c.CONDITIONID AS nvarchar(255))
                ),
                numbered AS (
                  SELECT
                    ROW_NUMBER() OVER (ORDER BY route_id) AS condition_occurrence_id,
                    *
                  FROM combined
                )
                INSERT INTO [{target_schema}].[condition_occurrence] (
                  condition_occurrence_id, person_id, condition_concept_id,
                  condition_start_date, condition_start_datetime,
                  condition_end_date, condition_end_datetime,
                  condition_type_concept_id, condition_status_concept_id,
                  provider_id, visit_occurrence_id, condition_source_value,
                  condition_source_concept_id, condition_status_source_value
                )
                SELECT
                  condition_occurrence_id, person_id, condition_concept_id,
                  condition_start_date, condition_start_datetime,
                  condition_end_date, condition_end_datetime,
                  condition_type_concept_id, condition_status_concept_id,
                  provider_id, visit_occurrence_id, condition_source_value,
                  condition_source_concept_id, condition_status_source_value
                FROM numbered;

                CREATE TABLE [{target_schema}].[{XWALK_TABLE}] (
                  route_id bigint NOT NULL,
                  source_domain varchar(16) NOT NULL,
                  source_record_id nvarchar(255) NOT NULL,
                  condition_occurrence_id bigint NOT NULL,
                  target_concept_id bigint NOT NULL,
                  route_status varchar(64) NOT NULL,
                  is_fallback bit NOT NULL,
                  source_code_type nvarchar(50) NULL,
                  source_provenance nvarchar(50) NULL,
                  date_basis varchar(32) NOT NULL,
                  CONSTRAINT PK_{XWALK_TABLE} PRIMARY KEY (route_id),
                  CONSTRAINT UQ_{XWALK_TABLE}_condition UNIQUE (condition_occurrence_id)
                );

                {eligible}
                , condition_routes AS (
                  SELECT *
                  FROM [{target_schema}].[{CANONICAL_ROUTE_TABLE}]
                  WHERE is_core_event_route = 1
                    AND target_domain = 'Condition'
                ),
                route_ids AS (
                  SELECT
                    r.route_id, r.source_domain, r.source_record_id,
                    r.target_concept_id, r.route_status, r.is_fallback,
                    r.source_code_type, r.source_provenance, r.date_basis
                  FROM condition_routes r
                  JOIN diag_eligible d
                    ON r.source_domain = 'DIAGNOSIS'
                   AND r.source_record_id = CAST(d.DIAGNOSISID AS nvarchar(255))
                  UNION ALL
                  SELECT
                    r.route_id, r.source_domain, r.source_record_id,
                    r.target_concept_id, r.route_status, r.is_fallback,
                    r.source_code_type, r.source_provenance, r.date_basis
                  FROM condition_routes r
                  JOIN cond_eligible c
                    ON r.source_domain = 'CONDITION'
                   AND r.source_record_id = CAST(c.CONDITIONID AS nvarchar(255))
                ),
                numbered_ids AS (
                  SELECT ROW_NUMBER() OVER (ORDER BY route_id) AS condition_occurrence_id, *
                  FROM route_ids
                )
                INSERT INTO [{target_schema}].[{XWALK_TABLE}] (
                  route_id, source_domain, source_record_id, condition_occurrence_id,
                  target_concept_id, route_status, is_fallback,
                  source_code_type, source_provenance, date_basis
                )
                SELECT
                  route_id, source_domain, source_record_id, condition_occurrence_id,
                  target_concept_id, route_status, is_fallback,
                  source_code_type, source_provenance, date_basis
                FROM numbered_ids;
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
            )
            lineage_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]",
            )
            if target_rows != expected_rows or lineage_rows != expected_rows:
                raise RuntimeError(
                    "Condition route reconciliation failed: "
                    f"routes={expected_rows:,}, target={target_rows:,}, lineage={lineage_rows:,}"
                )

            invalid_nonzero_type = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[condition_occurrence] co
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = co.condition_type_concept_id
                WHERE co.condition_type_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Type Concept'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            invalid_nonzero_status = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[condition_occurrence] co
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = co.condition_status_concept_id
                WHERE co.condition_status_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Condition Status'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_nonzero_type or invalid_nonzero_status:
                raise RuntimeError(
                    "Condition provenance concept integrity failed: "
                    f"invalid_type={invalid_nonzero_type:,}, invalid_status={invalid_nonzero_status:,}. "
                    "A clean rebuild is required; do not patch an existing target in place."
                )

            bad_lineage = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{XWALK_TABLE}] x
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id = x.condition_occurrence_id
                LEFT JOIN [{target_schema}].[{CANONICAL_ROUTE_TABLE}] r
                  ON r.route_id = x.route_id
                WHERE r.route_id IS NULL
                   OR r.target_domain <> 'Condition'
                   OR r.is_core_event_route <> 1
                   OR co.condition_concept_id <> r.target_concept_id
                   OR x.target_concept_id <> r.target_concept_id
                """,
            )
            if bad_lineage:
                raise RuntimeError(
                    f"Condition route-aware lineage has {bad_lineage:,} mismatched rows"
                )

            diagnosis_target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}] "
                "WHERE source_domain='DIAGNOSIS'",
            )
            condition_target_rows = target_rows - diagnosis_target_rows
            if diagnosis_target_rows != expected_diag_rows or condition_target_rows != expected_cond_rows:
                raise RuntimeError("Condition family route counts do not reconcile")

            def family_count(source_domain: str, predicate: str) -> int:
                return _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] co
                    JOIN [{target_schema}].[{XWALK_TABLE}] x
                      ON x.condition_occurrence_id = co.condition_occurrence_id
                    WHERE x.source_domain = '{source_domain}' AND {predicate}
                    """,
                )

            diagnosis_concept_zero = family_count(
                "DIAGNOSIS", "co.condition_concept_id = 0"
            )
            condition_concept_zero = family_count(
                "CONDITION", "co.condition_concept_id = 0"
            )
            diagnosis_source_concept_zero = family_count(
                "DIAGNOSIS", "co.condition_source_concept_id = 0"
            )
            condition_source_concept_zero = family_count(
                "CONDITION", "co.condition_source_concept_id = 0"
            )
            diagnosis_visit_linked = family_count(
                "DIAGNOSIS", "co.visit_occurrence_id IS NOT NULL"
            )
            condition_visit_linked = family_count(
                "CONDITION", "co.visit_occurrence_id IS NOT NULL"
            )

    finally:
        engine.dispose()

    result = ConditionOccurrenceTransformResult(
        diagnosis_source_rows=diagnosis_source_rows,
        diagnosis_eligible_rows=diagnosis_eligible_rows,
        diagnosis_excluded_rows=diagnosis_excluded_rows,
        diagnosis_missing_id=diagnosis_missing_id,
        diagnosis_missing_patid=diagnosis_missing_patid,
        diagnosis_unlinked_person=diagnosis_unlinked_person,
        diagnosis_missing_dx_date=diagnosis_missing_dx_date,
        condition_source_rows=condition_source_rows,
        condition_eligible_rows=condition_eligible_rows,
        condition_excluded_rows=condition_excluded_rows,
        condition_missing_id=condition_missing_id,
        condition_missing_patid=condition_missing_patid,
        condition_unlinked_person=condition_unlinked_person,
        condition_missing_date=condition_missing_date,
        condition_report_date_fallback=condition_report_date_fallback,
        condition_invalid_interval=condition_invalid_interval,
        target_rows=target_rows,
        diagnosis_target_rows=diagnosis_target_rows,
        condition_target_rows=condition_target_rows,
        diagnosis_concept_zero=diagnosis_concept_zero,
        condition_concept_zero=condition_concept_zero,
        diagnosis_source_concept_zero=diagnosis_source_concept_zero,
        condition_source_concept_zero=condition_source_concept_zero,
        diagnosis_visit_linked=diagnosis_visit_linked,
        condition_visit_linked=condition_visit_linked,
        lineage_rows=lineage_rows,
        status=status,
        audit_path=audit_path,
    )

    audit_payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "condition_occurrence",
        "sources": [
            f"{source_schema}.PCORnet_DIAGNOSIS",
            f"{source_schema}.PCORnet_CONDITION",
        ],
        "target_table": f"{target_schema}.condition_occurrence",
        "canonical_route_table": f"{target_schema}.{CANONICAL_ROUTE_TABLE}",
        "lineage_table": f"{target_schema}.{XWALK_TABLE}",
        "policies": {
            "condition_sources": policies.get("condition_sources"),
            "missing_required_date": policies.get("missing_required_date"),
            "unmapped_standard_concept": policies.get("unmapped_standard_concept"),
        },
        "mapping_strategy": {
            "canonical_routing": (
                "Materialize every canonical core-event route whose target domain is Condition; "
                "one source event may therefore produce multiple condition_occurrence rows."
            ),
            "fallback": (
                "If no core event-domain Standard target exists, canonical routing contributes one "
                "Condition concept_id 0 row preserving source clinical-event semantics."
            ),
            "cross_domain": (
                "Source events mapped only to other clinical event domains are not duplicated into "
                "condition_occurrence; their routes are materialized by domain-specific stages."
            ),
            "maps_to_value": "Never treated as an independent clinical event route.",
            "source_concept_resolution": (
                "Owned by the canonical route ledger; no TOP(1) source or target selection occurs here."
            ),
            "diagnosis_date": "DX_DATE required; missing dates excluded without sentinel.",
            "condition_date": (
                "ONSET_DATE when available, otherwise REPORT_DATE; both absent excluded."
            ),
            "condition_end": (
                "RESOLVE_DATE when present; intervals ending before the effective start are excluded."
            ),
            "source_lineage": (
                "Route-aware lineage preserves source event identity plus canonical route_id; "
                "DIAGNOSIS and CONDITION remain separate with no silent deduplication."
            ),
            "visit_linkage": (
                "ENCOUNTERID linked only when the encounter survived validated visit_occurrence ETL."
            ),
            "provider": "NULL because no validated provider mapping is assigned.",
            "condition_provenance": (
                "CONDITION_TYPE/CONDITION_SOURCE retained in lineage; OMOP type/status concepts remain 0 "
                "unless source-established semantics map to an active Standard concept in the exact expected domain."
            ),
            "diagnosis_type_expected_domain": "Type Concept",
            "diagnosis_status_expected_domain": "Condition Status",
            "diagnosis_rejected_type_concepts": rejected_type_map,
            "diagnosis_rejected_status_concepts": rejected_status_map,
        },
        "result": {**asdict(result), "audit_path": str(audit_path)},
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(audit_payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return result
