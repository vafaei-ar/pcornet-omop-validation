from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


# Historical PCORnet provenance/status mappings are retained only when the
# referenced concept exists in the loaded vocabulary. Unknown or obsolete
# concepts become 0.
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


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_CONDITION"),
        (target_schema, "person"),
        (target_schema, "condition_occurrence"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
        (target_schema, "etl_visit_occurrence_xwalk"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(
                f"Required table [{schema}].[{table}] does not exist"
            )


def _validated_existing_map(
    connection,
    schema: str,
    mapping: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    ids = sorted(set(mapping.values()))
    if not ids:
        return {}, mapping
    values = ",".join(str(value) for value in ids)
    present = {
        int(row[0])
        for row in connection.execute(
            text(
                f"SELECT concept_id FROM [{schema}].[concept] "
                f"WHERE concept_id IN ({values})"
            )
        ).fetchall()
    }
    valid = {key: value for key, value in mapping.items() if value in present}
    rejected = {
        key: value for key, value in mapping.items() if value not in present
    }
    return valid, rejected


def _case_sql(column: str, mapping: dict[str, int]) -> str:
    clauses = " ".join(
        f"WHEN {column} = '{key}' THEN {value}"
        for key, value in mapping.items()
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
    ),
    source_codes AS (
      SELECT DISTINCT CAST(DX AS nvarchar(255)) AS source_code, vocabulary_id
      FROM diag_eligible
      WHERE DX IS NOT NULL AND vocabulary_id IS NOT NULL
      UNION
      SELECT DISTINCT CAST(CONDITION AS nvarchar(255)), vocabulary_id
      FROM cond_eligible
      WHERE CONDITION IS NOT NULL AND vocabulary_id IS NOT NULL
    ),
    code_map AS (
      SELECT
        sc.source_code,
        sc.vocabulary_id,
        src.concept_id AS source_concept_id,
        CASE
          WHEN src.standard_concept = 'S'
           AND src.domain_id = 'Condition'
           AND src.invalid_reason IS NULL
            THEN src.concept_id
          WHEN mapped.target_count = 1
            THEN mapped.unique_target_concept_id
          ELSE NULL
        END AS standard_concept_id,
        COALESCE(mapped.target_count, 0) AS condition_target_count
      FROM source_codes sc
      OUTER APPLY (
        SELECT TOP (1)
          c.concept_id,
          c.standard_concept,
          c.domain_id,
          c.invalid_reason
        FROM [{target_schema}].[concept] c
        WHERE c.concept_code = sc.source_code
          AND c.vocabulary_id = sc.vocabulary_id
        ORDER BY
          CASE WHEN c.invalid_reason IS NULL THEN 0 ELSE 1 END,
          c.concept_id
      ) src
      OUTER APPLY (
        SELECT
          COUNT(DISTINCT target.concept_id) AS target_count,
          CASE
            WHEN COUNT(DISTINCT target.concept_id) = 1
              THEN MAX(target.concept_id)
            ELSE NULL
          END AS unique_target_concept_id
        FROM [{target_schema}].[concept_relationship] cr
        JOIN [{target_schema}].[concept] target
          ON target.concept_id = cr.concept_id_2
        WHERE cr.concept_id_1 = src.concept_id
          AND cr.relationship_id = 'Maps to'
          AND target.standard_concept = 'S'
          AND target.domain_id = 'Condition'
          AND target.invalid_reason IS NULL
          AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
      ) mapped
    )
    """


def transform_condition_occurrence(
    config: EtlConfig,
) -> ConditionOccurrenceTransformResult:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "The validated condition stage requires "
            "policies.missing_required_date=exclude"
        )
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "The validated condition stage requires "
            "policies.unmapped_standard_concept=concept_zero"
        )
    if policies.get("condition_sources") != "include_both":
        raise RuntimeError(
            "The validated condition stage requires "
            "policies.condition_sources=include_both"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "condition_occurrence_transform.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            diagnosis_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM "
                f"[{source_schema}].[PCORnet_DIAGNOSIS]",
            )
            condition_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM "
                f"[{source_schema}].[PCORnet_CONDITION]",
            )

            diag_cte = f"""
            WITH classified AS (
              SELECT d.*, p.person_id,
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
                        + " SELECT record_status, COUNT_BIG(*) "
                          "FROM classified GROUP BY record_status"
                    )
                ).fetchall()
            )
            diagnosis_eligible_rows = int(diag_counts.get("eligible", 0))
            diagnosis_missing_id = int(diag_counts.get("missing_id", 0))
            diagnosis_missing_patid = int(
                diag_counts.get("missing_patid", 0)
            )
            diagnosis_unlinked_person = int(
                diag_counts.get("unlinked_person", 0)
            )
            diagnosis_missing_dx_date = int(
                diag_counts.get("missing_dx_date", 0)
            )
            diagnosis_excluded_rows = (
                diagnosis_source_rows - diagnosis_eligible_rows
            )

            cond_cte = f"""
            WITH classified AS (
              SELECT c.*, p.person_id,
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
                        + " SELECT record_status, COUNT_BIG(*) "
                          "FROM classified GROUP BY record_status"
                    )
                ).fetchall()
            )
            condition_eligible_rows = int(cond_counts.get("eligible", 0))
            condition_missing_id = int(cond_counts.get("missing_id", 0))
            condition_missing_patid = int(
                cond_counts.get("missing_patid", 0)
            )
            condition_unlinked_person = int(
                cond_counts.get("unlinked_person", 0)
            )
            condition_missing_date = int(cond_counts.get("missing_date", 0))
            condition_invalid_interval = int(
                cond_counts.get("invalid_interval", 0)
            )
            condition_excluded_rows = (
                condition_source_rows - condition_eligible_rows
            )
            condition_report_date_fallback = _scalar(
                connection,
                cond_cte
                + " SELECT COUNT_BIG(*) FROM classified "
                  "WHERE record_status='eligible' "
                  "AND ONSET_DATE IS NULL AND REPORT_DATE IS NOT NULL",
            )

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

            type_map, rejected_type_map = _validated_existing_map(
                connection, target_schema, DX_ORIGIN_TYPE_MAP
            )
            status_map, rejected_status_map = _validated_existing_map(
                connection, target_schema, DX_SOURCE_STATUS_MAP
            )

            expected_rows = diagnosis_eligible_rows + condition_eligible_rows
            existing = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM "
                f"[{target_schema}].[condition_occurrence]",
            )
            if existing:
                if existing != expected_rows:
                    raise RuntimeError(
                        f"Target [{target_schema}].[condition_occurrence] already "
                        f"has {existing:,} rows; validated eligible total is "
                        f"{expected_rows:,}. Refusing to append or overwrite."
                    )
                if not table_exists(connection, target_schema, XWALK_TABLE):
                    raise RuntimeError(
                        "Condition target exists but lineage table is missing"
                    )
                status = "already_loaded_matched"
            else:
                if table_exists(connection, target_schema, XWALK_TABLE):
                    raise RuntimeError(
                        f"[{target_schema}].[{XWALK_TABLE}] exists while "
                        "condition_occurrence is empty; refusing partial-state load"
                    )

                type_case = _case_sql("DX_ORIGIN", type_map)
                status_case = _case_sql("DX_SOURCE", status_map)
                ctes = _eligible_ctes(source_schema, target_schema)

                insert_sql = ctes + f"""
                , combined AS (
                  SELECT
                    'DIAGNOSIS' AS source_domain,
                    CAST(d.DIAGNOSISID AS nvarchar(255)) AS source_record_id,
                    d.person_id,
                    COALESCE(cm.standard_concept_id, 0) AS condition_concept_id,
                    CAST(d.DX_DATE AS date) AS condition_start_date,
                    CAST(d.DX_DATE AS datetime2(7)) AS condition_start_datetime,
                    CAST(NULL AS date) AS condition_end_date,
                    CAST(NULL AS datetime2(7)) AS condition_end_datetime,
                    {type_case} AS condition_type_concept_id,
                    {status_case} AS condition_status_concept_id,
                    CAST(NULL AS bigint) AS provider_id,
                    d.visit_occurrence_id,
                    CAST(d.DX AS nvarchar(255)) AS condition_source_value,
                    COALESCE(cm.source_concept_id, 0) AS condition_source_concept_id,
                    CAST(d.DX_SOURCE AS nvarchar(50)) AS condition_status_source_value,
                    CAST(d.DX_TYPE AS nvarchar(50)) AS source_code_type,
                    CAST(d.DX_ORIGIN AS nvarchar(50)) AS source_provenance,
                    CAST('DX_DATE' AS nvarchar(32)) AS date_basis
                  FROM diag_eligible d
                  LEFT JOIN code_map cm
                    ON cm.source_code = CAST(d.DX AS nvarchar(255))
                   AND cm.vocabulary_id = d.vocabulary_id

                  UNION ALL

                  SELECT
                    'CONDITION',
                    CAST(c.CONDITIONID AS nvarchar(255)),
                    c.person_id,
                    COALESCE(cm.standard_concept_id, 0),
                    CAST(c.effective_start_date AS date),
                    CAST(c.effective_start_date AS datetime2(7)),
                    CAST(c.RESOLVE_DATE AS date),
                    CAST(c.RESOLVE_DATE AS datetime2(7)),
                    0,
                    0,
                    CAST(NULL AS bigint),
                    c.visit_occurrence_id,
                    CAST(c.CONDITION AS nvarchar(255)),
                    COALESCE(cm.source_concept_id, 0),
                    CAST(c.CONDITION_STATUS AS nvarchar(50)),
                    CAST(c.CONDITION_TYPE AS nvarchar(50)),
                    CAST(c.CONDITION_SOURCE AS nvarchar(50)),
                    CASE
                      WHEN c.ONSET_DATE IS NOT NULL THEN 'ONSET_DATE'
                      ELSE 'REPORT_DATE'
                    END
                  FROM cond_eligible c
                  LEFT JOIN code_map cm
                    ON cm.source_code = CAST(c.CONDITION AS nvarchar(255))
                   AND cm.vocabulary_id = c.vocabulary_id
                ),
                numbered AS (
                  SELECT
                    ROW_NUMBER() OVER (
                      ORDER BY source_domain, source_record_id
                    ) AS condition_occurrence_id,
                    *
                  FROM combined
                )
                INSERT INTO [{target_schema}].[condition_occurrence] (
                  condition_occurrence_id,
                  person_id,
                  condition_concept_id,
                  condition_start_date,
                  condition_start_datetime,
                  condition_end_date,
                  condition_end_datetime,
                  condition_type_concept_id,
                  condition_status_concept_id,
                  provider_id,
                  visit_occurrence_id,
                  condition_source_value,
                  condition_source_concept_id,
                  condition_status_source_value
                )
                SELECT
                  condition_occurrence_id,
                  person_id,
                  condition_concept_id,
                  condition_start_date,
                  condition_start_datetime,
                  condition_end_date,
                  condition_end_datetime,
                  condition_type_concept_id,
                  condition_status_concept_id,
                  provider_id,
                  visit_occurrence_id,
                  condition_source_value,
                  condition_source_concept_id,
                  condition_status_source_value
                FROM numbered;

                CREATE TABLE [{target_schema}].[{XWALK_TABLE}] (
                  source_domain varchar(16) NOT NULL,
                  source_record_id nvarchar(255) NOT NULL,
                  condition_occurrence_id bigint NOT NULL,
                  source_code_type nvarchar(50) NULL,
                  source_provenance nvarchar(50) NULL,
                  date_basis varchar(32) NOT NULL,
                  CONSTRAINT PK_{XWALK_TABLE}
                    PRIMARY KEY (source_domain, source_record_id),
                  CONSTRAINT UQ_{XWALK_TABLE}_condition
                    UNIQUE (condition_occurrence_id)
                );

                WITH diag_ids AS (
                  SELECT
                    CAST(DIAGNOSISID AS nvarchar(255)) AS source_record_id,
                    CAST(DX_TYPE AS nvarchar(50)) AS source_code_type,
                    CAST(DX_ORIGIN AS nvarchar(50)) AS source_provenance,
                    CAST('DX_DATE' AS varchar(32)) AS date_basis
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  JOIN [{target_schema}].[person] p
                    ON CAST(d.PATID AS nvarchar(50)) = p.person_source_value
                  WHERE d.DIAGNOSISID IS NOT NULL
                    AND LTRIM(RTRIM(CAST(d.DIAGNOSISID AS nvarchar(max)))) <> ''
                    AND d.DX_DATE IS NOT NULL
                ),
                cond_ids AS (
                  SELECT
                    CAST(CONDITIONID AS nvarchar(255)) AS source_record_id,
                    CAST(CONDITION_TYPE AS nvarchar(50)) AS source_code_type,
                    CAST(CONDITION_SOURCE AS nvarchar(50)) AS source_provenance,
                    CAST(
                      CASE
                        WHEN ONSET_DATE IS NOT NULL THEN 'ONSET_DATE'
                        ELSE 'REPORT_DATE'
                      END AS varchar(32)
                    ) AS date_basis
                  FROM [{source_schema}].[PCORnet_CONDITION] c
                  JOIN [{target_schema}].[person] p
                    ON CAST(c.PATID AS nvarchar(50)) = p.person_source_value
                  WHERE c.CONDITIONID IS NOT NULL
                    AND LTRIM(RTRIM(CAST(c.CONDITIONID AS nvarchar(max)))) <> ''
                    AND COALESCE(c.ONSET_DATE, c.REPORT_DATE) IS NOT NULL
                    AND (
                      c.RESOLVE_DATE IS NULL
                      OR CAST(c.RESOLVE_DATE AS date) >=
                         CAST(COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS date)
                    )
                ),
                combined_ids AS (
                  SELECT 'DIAGNOSIS' AS source_domain, * FROM diag_ids
                  UNION ALL
                  SELECT 'CONDITION', * FROM cond_ids
                ),
                numbered_ids AS (
                  SELECT
                    ROW_NUMBER() OVER (
                      ORDER BY source_domain, source_record_id
                    ) AS condition_occurrence_id,
                    *
                  FROM combined_ids
                )
                INSERT INTO [{target_schema}].[{XWALK_TABLE}] (
                  source_domain,
                  source_record_id,
                  condition_occurrence_id,
                  source_code_type,
                  source_provenance,
                  date_basis
                )
                SELECT
                  source_domain,
                  source_record_id,
                  condition_occurrence_id,
                  source_code_type,
                  source_provenance,
                  date_basis
                FROM numbered_ids;
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM "
                f"[{target_schema}].[condition_occurrence]",
            )
            if target_rows != expected_rows:
                raise RuntimeError(
                    "Condition reconciliation failed: "
                    f"eligible={expected_rows:,}, target={target_rows:,}"
                )

            if not table_exists(connection, target_schema, XWALK_TABLE):
                raise RuntimeError(
                    "Condition lineage table is missing after transformation"
                )
            lineage_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM "
                f"[{target_schema}].[{XWALK_TABLE}]",
            )
            if lineage_rows != target_rows:
                raise RuntimeError(
                    "Condition lineage reconciliation failed: "
                    f"lineage={lineage_rows:,}, target={target_rows:,}"
                )

            diagnosis_target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}] "
                "WHERE source_domain='DIAGNOSIS'",
            )
            condition_target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}] "
                "WHERE source_domain='CONDITION'",
            )

            def family_count(source_domain: str, predicate: str) -> int:
                return _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] co
                    JOIN [{target_schema}].[{XWALK_TABLE}] x
                      ON x.condition_occurrence_id = co.condition_occurrence_id
                    WHERE x.source_domain = '{source_domain}'
                      AND {predicate}
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
        "lineage_table": f"{target_schema}.{XWALK_TABLE}",
        "policies": {
            "condition_sources": policies.get("condition_sources"),
            "missing_required_date": policies.get("missing_required_date"),
            "unmapped_standard_concept": policies.get(
                "unmapped_standard_concept"
            ),
        },
        "mapping_strategy": {
            "diagnosis_date": (
                "DX_DATE required; missing DX_DATE excluded without sentinel"
            ),
            "condition_date": (
                "ONSET_DATE when available; otherwise REPORT_DATE as an "
                "explicit source-domain fallback; both absent excluded"
            ),
            "condition_end": (
                "RESOLVE_DATE when present; rows resolving before effective "
                "start are excluded"
            ),
            "source_vocabulary": (
                "DX_TYPE/CONDITION_TYPE routed explicitly to ICD9CM, "
                "ICD10CM, or SNOMED; unsupported types remain unmapped"
            ),
            "standard_mapping": (
                "Use an active Standard Condition source concept directly; "
                "otherwise assign a mapped Condition concept only when exactly "
                "one active Standard Condition target exists. Zero or multiple "
                "Condition targets remain concept_id 0 for downstream canonical "
                "routing; no TOP(1) target selection is permitted."
            ),
            "source_lineage": (
                "DIAGNOSIS and CONDITION retained separately; no silent "
                "deduplication"
            ),
            "visit_linkage": (
                "ENCOUNTERID linked only when the encounter survived validated "
                "visit_occurrence ETL; otherwise visit_occurrence_id is NULL"
            ),
            "provider": "NULL because no validated provider mapping is assigned",
            "condition_provenance": (
                "CONDITION_TYPE/CONDITION_SOURCE retained in lineage; OMOP "
                "type/status concepts left 0 pending semantic validation"
            ),
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
