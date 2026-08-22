from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_measurement_xwalk"
PROCEDURE_ROUTE_TABLE = "etl_procedure_event_route"
VISIT_XWALK_TABLE = "etl_visit_occurrence_xwalk"

# PCORnet VITAL fields have fixed clinical semantics. OMOP concept ids are
# resolved from the loaded vocabulary at run time rather than frozen here.
VITAL_SPECS = {
    "HT": ("8302-2", "[in_i]"),
    "WT": ("29463-7", "[lb_av]"),
    "SYSTOLIC": ("8480-6", "mm[Hg]"),
    "DIASTOLIC": ("8462-4", "mm[Hg]"),
    "ORIGINAL_BMI": ("39156-5", "kg/m2"),
}


@dataclass(frozen=True)
class MeasurementTransformResult:
    lab_source_rows: int
    lab_measurement_rows: int
    lab_direct_standard_rows: int
    lab_mapped_rows: int
    lab_observation_domain_rows: int
    vital_source_rows: int
    vital_measurement_rows: int
    procedure_measurement_rows: int
    expected_rows: int
    target_rows: int
    lineage_rows: int
    lab_visit_linked_rows: int
    vital_visit_linked_rows: int
    procedure_visit_linked_rows: int
    lab_unit_concept_zero_rows: int
    target_concept_zero_rows: int
    status: str
    audit_path: Path


def _validated_schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    value = connection.execute(text(sql), params or {}).scalar_one()
    return int(value or 0)


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    source_required = (
        "PCORnet_LAB_RESULT_CM",
        "PCORnet_VITAL",
    )
    target_required = (
        PROCEDURE_ROUTE_TABLE,
        VISIT_XWALK_TABLE,
        "person",
        "measurement",
        "concept",
        "concept_relationship",
    )
    for table in source_required:
        if not table_exists(connection, source_schema, table):
            raise RuntimeError(
                f"Required table [{source_schema}].[{table}] does not exist"
            )
    for table in target_required:
        if not table_exists(connection, target_schema, table):
            raise RuntimeError(
                f"Required table [{target_schema}].[{table}] does not exist"
            )


def _safe_datetime_sql(date_column: str, time_column: str) -> str:
    """Combine date with PCORnet numeric seconds-since-midnight."""
    seconds = f"TRY_CONVERT(float, {time_column})"
    return f"""
    CASE
      WHEN {date_column} IS NULL THEN NULL
      WHEN {seconds} IS NULL
        OR {seconds} < 0
        OR {seconds} >= 86400
        THEN CAST(CAST({date_column} AS date) AS datetime2(7))
      ELSE DATEADD(
        MILLISECOND,
        CAST(ROUND({seconds} * 1000.0, 0) AS bigint),
        CAST(CAST({date_column} AS date) AS datetime2(7))
      )
    END
    """.strip()


def _lab_mapping_cte(source_schema: str, target_schema: str) -> str:
    """Resolve LAB_LOINC without arbitrary source or target selection.

    Rules are vocabulary-driven and independent of downstream agreement:
      * prefer a unique active exact LOINC source concept;
      * if no active exact concept exists, permit a unique invalid exact source
        concept so active Maps to relationships can be followed;
      * direct active Standard Measurement concepts map to themselves;
      * active Standard concepts in another domain are routed outside
        Measurement;
      * otherwise use a mapped active Standard Measurement concept only when
        exactly one target exists;
      * unresolved or ambiguous LAB events remain Measurement with concept 0.
    """
    return f"""
    WITH lab_codes AS (
      SELECT DISTINCT
        LTRIM(RTRIM(CONVERT(nvarchar(100), LAB_LOINC))) AS loinc
      FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
      WHERE RESULT_DATE IS NOT NULL
    ),
    source_candidates AS (
      SELECT
        lc.loinc,
        c.concept_id,
        c.domain_id,
        c.standard_concept,
        c.invalid_reason
      FROM lab_codes lc
      LEFT JOIN [{target_schema}].[concept] c
        ON c.vocabulary_id = 'LOINC'
       AND c.concept_code = lc.loinc
    ),
    source_counts AS (
      SELECT
        loinc,
        SUM(CASE WHEN concept_id IS NOT NULL THEN 1 ELSE 0 END) AS total_count,
        SUM(CASE WHEN concept_id IS NOT NULL AND invalid_reason IS NULL THEN 1 ELSE 0 END)
          AS active_count
      FROM source_candidates
      GROUP BY loinc
    ),
    source_choice AS (
      SELECT
        sc.loinc,
        CASE
          WHEN sc.active_count = 1 THEN MAX(
            CASE WHEN c.invalid_reason IS NULL THEN c.concept_id END
          )
          WHEN sc.active_count = 0 AND sc.total_count = 1 THEN MAX(c.concept_id)
          ELSE NULL
        END AS source_concept_id,
        sc.total_count,
        sc.active_count
      FROM source_counts sc
      LEFT JOIN source_candidates c
        ON c.loinc = sc.loinc
      GROUP BY sc.loinc, sc.total_count, sc.active_count
    ),
    selected_source AS (
      SELECT
        ch.loinc,
        ch.source_concept_id,
        ch.total_count,
        ch.active_count,
        c.domain_id AS source_domain,
        c.standard_concept AS source_standard_concept,
        c.invalid_reason AS source_invalid_reason
      FROM source_choice ch
      LEFT JOIN [{target_schema}].[concept] c
        ON c.concept_id = ch.source_concept_id
    ),
    mapped_targets AS (
      SELECT DISTINCT
        ss.loinc,
        tgt.concept_id AS target_concept_id
      FROM selected_source ss
      JOIN [{target_schema}].[concept_relationship] cr
        ON cr.concept_id_1 = ss.source_concept_id
       AND cr.relationship_id = 'Maps to'
       AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
      JOIN [{target_schema}].[concept] tgt
        ON tgt.concept_id = cr.concept_id_2
       AND tgt.standard_concept = 'S'
       AND tgt.invalid_reason IS NULL
       AND tgt.domain_id = 'Measurement'
      WHERE NOT (
        ss.source_invalid_reason IS NULL
        AND COALESCE(ss.source_standard_concept, '') = 'S'
      )
    ),
    mapped_counts AS (
      SELECT loinc, COUNT(DISTINCT target_concept_id) AS n_targets
      FROM mapped_targets
      GROUP BY loinc
    ),
    unique_target AS (
      SELECT mt.loinc, MAX(mt.target_concept_id) AS target_concept_id
      FROM mapped_targets mt
      JOIN mapped_counts mc
        ON mc.loinc = mt.loinc
       AND mc.n_targets = 1
      GROUP BY mt.loinc
    ),
    lab_map AS (
      SELECT
        ss.loinc,
        COALESCE(ss.source_concept_id, 0) AS source_concept_id,
        CASE
          WHEN ss.source_invalid_reason IS NULL
           AND ss.source_standard_concept = 'S'
           AND ss.source_domain = 'Measurement'
            THEN ss.source_concept_id
          WHEN ss.source_invalid_reason IS NULL
           AND ss.source_standard_concept = 'S'
           AND ss.source_domain <> 'Measurement'
            THEN NULL
          WHEN mc.n_targets = 1
            THEN ut.target_concept_id
          ELSE 0
        END AS measurement_concept_id,
        CASE
          WHEN ss.source_invalid_reason IS NULL
           AND ss.source_standard_concept = 'S'
           AND ss.source_domain = 'Measurement'
            THEN 'direct_standard_measurement'
          WHEN ss.source_invalid_reason IS NULL
           AND ss.source_standard_concept = 'S'
           AND ss.source_domain <> 'Measurement'
            THEN 'standard_other_domain'
          WHEN mc.n_targets = 1
            THEN 'maps_to_standard_measurement'
          WHEN ss.source_concept_id IS NULL AND ss.total_count > 1
            THEN 'ambiguous_source_concept_zero'
          WHEN mc.n_targets > 1
            THEN 'multiple_measurement_targets_zero'
          WHEN ss.source_concept_id IS NULL
            THEN 'source_concept_not_found_zero'
          ELSE 'no_active_measurement_target_zero'
        END AS route_status
      FROM selected_source ss
      LEFT JOIN mapped_counts mc
        ON mc.loinc = ss.loinc
      LEFT JOIN unique_target ut
        ON ut.loinc = ss.loinc
    )
    """


def _resolve_unique_standard_concept(
    connection,
    target_schema: str,
    *,
    vocabulary_id: str,
    domain_id: str,
    concept_code: str,
    case_sensitive: bool = False,
) -> int:
    collation = " COLLATE Latin1_General_100_BIN2" if case_sensitive else ""
    rows = connection.execute(
        text(
            f"""
            SELECT concept_id
            FROM [{target_schema}].[concept]
            WHERE vocabulary_id = :vocabulary_id
              AND domain_id = :domain_id
              AND standard_concept = 'S'
              AND invalid_reason IS NULL
              AND concept_code{collation} = :concept_code{collation}
            """
        ),
        {
            "vocabulary_id": vocabulary_id,
            "domain_id": domain_id,
            "concept_code": concept_code,
        },
    ).fetchall()
    if len(rows) == 0:
        return 0
    if len(rows) > 1:
        return 0
    return int(rows[0][0])


def _resolve_vital_specs(connection, target_schema: str) -> dict[str, tuple[int, int, str]]:
    resolved: dict[str, tuple[int, int, str]] = {}
    for field, (loinc, unit_code) in VITAL_SPECS.items():
        measurement_concept_id = _resolve_unique_standard_concept(
            connection,
            target_schema,
            vocabulary_id="LOINC",
            domain_id="Measurement",
            concept_code=loinc,
        )
        if measurement_concept_id == 0:
            raise RuntimeError(
                f"VITAL field {field} does not resolve to exactly one active "
                f"Standard Measurement LOINC {loinc}"
            )
        unit_concept_id = _resolve_unique_standard_concept(
            connection,
            target_schema,
            vocabulary_id="UCUM",
            domain_id="Unit",
            concept_code=unit_code,
            case_sensitive=True,
        )
        resolved[field] = (
            measurement_concept_id,
            unit_concept_id,
            unit_code,
        )
    return resolved


def transform_measurement(config: EtlConfig) -> MeasurementTransformResult:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "Validated Measurement ETL requires "
            "policies.unmapped_standard_concept=concept_zero"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = _validated_schema(
        sql_cfg.get("source_schema", "dbo"), "source_schema"
    )
    target_schema = _validated_schema(
        sql_cfg.get("target_schema", "dbo"), "target_schema"
    )
    audit_path = config.audit_dir / "measurement_transform.json"

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            vital_specs = _resolve_vital_specs(connection, target_schema)

            lab_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {s('PCORnet_LAB_RESULT_CM')}",
            )
            lab_missing_result_date = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {s('PCORnet_LAB_RESULT_CM')} "
                "WHERE RESULT_DATE IS NULL",
            )
            vital_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {s('PCORnet_VITAL')}",
            )

            lab_person_unlinked = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {s('PCORnet_LAB_RESULT_CM')} l
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), l.PATID)))
                WHERE l.RESULT_DATE IS NOT NULL
                  AND p.person_id IS NULL
                """,
            )
            vital_person_unlinked = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {s('PCORnet_VITAL')} v
                CROSS APPLY (VALUES
                    (v.HT), (v.WT), (v.SYSTOLIC), (v.DIASTOLIC), (v.ORIGINAL_BMI)
                ) x(source_value)
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), v.PATID)))
                WHERE x.source_value IS NOT NULL
                  AND v.MEASURE_DATE IS NOT NULL
                  AND p.person_id IS NULL
                """,
            )
            if lab_person_unlinked or vital_person_unlinked:
                raise RuntimeError(
                    "Measurement source rows have unlinked persons: "
                    f"LAB={lab_person_unlinked:,}, VITAL={vital_person_unlinked:,}"
                )

            lab_map_cte = _lab_mapping_cte(source_schema, target_schema)
            lab_route_counts = dict(
                connection.execute(
                    text(
                        lab_map_cte
                        + f"""
                        SELECT lm.route_status, COUNT_BIG(*) AS n
                        FROM {s('PCORnet_LAB_RESULT_CM')} l
                        JOIN lab_map lm
                          ON lm.loinc = LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC)))
                        WHERE l.RESULT_DATE IS NOT NULL
                        GROUP BY lm.route_status
                        """
                    )
                ).fetchall()
            )

            lab_direct_standard_rows = int(
                lab_route_counts.get("direct_standard_measurement", 0)
            )
            lab_mapped_rows = int(
                lab_route_counts.get("maps_to_standard_measurement", 0)
            )
            lab_observation_domain_rows = int(
                lab_route_counts.get("standard_other_domain", 0)
            )
            lab_concept_zero_rows = sum(
                int(v)
                for k, v in lab_route_counts.items()
                if str(k).endswith("_zero")
            )
            lab_measurement_rows = (
                lab_direct_standard_rows + lab_mapped_rows + lab_concept_zero_rows
            )

            vital_expanded_total = _scalar(
                connection,
                f"""
                SELECT SUM(
                    CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                )
                FROM {s('PCORnet_VITAL')}
                """,
            )
            vital_missing_measure_date = _scalar(
                connection,
                f"""
                SELECT SUM(
                    CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                    CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                )
                FROM {s('PCORnet_VITAL')}
                WHERE MEASURE_DATE IS NULL
                """,
            )
            vital_measurement_rows = vital_expanded_total - vital_missing_measure_date

            procedure_measurement_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(PROCEDURE_ROUTE_TABLE)}
                WHERE target_domain = 'Measurement'
                  AND px_date IS NOT NULL
                """,
            )
            procedure_missing_px_date = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(PROCEDURE_ROUTE_TABLE)}
                WHERE target_domain = 'Measurement'
                  AND px_date IS NULL
                """,
            )
            procedure_person_unlinked = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(PROCEDURE_ROUTE_TABLE)} r
                LEFT JOIN {t('person')} p
                  ON p.person_source_value = r.patid
                WHERE r.target_domain = 'Measurement'
                  AND r.px_date IS NOT NULL
                  AND p.person_id IS NULL
                """,
            )
            if procedure_person_unlinked:
                raise RuntimeError(
                    "Procedure-derived Measurement routes have unlinked persons: "
                    f"{procedure_person_unlinked:,}"
                )

            invalid_procedure_targets = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(PROCEDURE_ROUTE_TABLE)} r
                LEFT JOIN {t('concept')} c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_domain = 'Measurement'
                  AND r.px_date IS NOT NULL
                  AND COALESCE(r.target_concept_id, 0) <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.standard_concept <> 'S'
                    OR c.domain_id <> 'Measurement'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_procedure_targets:
                raise RuntimeError(
                    f"{invalid_procedure_targets:,} procedure-derived "
                    "Measurement targets are invalid"
                )

            expected_rows = (
                lab_measurement_rows
                + vital_measurement_rows
                + procedure_measurement_rows
            )

            xwalk_exists = table_exists(connection, target_schema, XWALK_TABLE)
            existing_target = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {t('measurement')}",
            )

            if xwalk_exists:
                lineage_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}",
                )
                if existing_target == expected_rows and lineage_rows == expected_rows:
                    status = "already_loaded_matched"
                else:
                    raise RuntimeError(
                        "Measurement base stage is not in a reconciled idempotent state: "
                        f"expected={expected_rows:,}, target={existing_target:,}, "
                        f"lineage={lineage_rows:,}. Refusing to mutate."
                    )
            elif existing_target:
                raise RuntimeError(
                    f"Target {t('measurement')} already contains {existing_target:,} "
                    "rows without Measurement lineage. Refusing to mutate."
                )
            else:
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE {t(XWALK_TABLE)} (
                        measurement_id bigint NOT NULL,
                        source_family varchar(32) NOT NULL,
                        source_record_id nvarchar(255) NOT NULL,
                        source_field varchar(32) NULL,
                        source_route_id bigint NULL,
                        CONSTRAINT PK_{XWALK_TABLE}
                            PRIMARY KEY (measurement_id)
                    )
                    """
                )
                connection.commit()

                connection.exec_driver_sql(
                    lab_map_cte
                    + f"""
                    INSERT INTO {t(XWALK_TABLE)} (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        ROW_NUMBER() OVER (
                            ORDER BY LTRIM(RTRIM(CONVERT(nvarchar(255), l.LAB_RESULT_CM_ID)))
                        ),
                        'LAB_RESULT_CM',
                        LTRIM(RTRIM(CONVERT(nvarchar(255), l.LAB_RESULT_CM_ID))),
                        NULL,
                        NULL
                    FROM {s('PCORnet_LAB_RESULT_CM')} l
                    JOIN lab_map lm
                      ON lm.loinc = LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC)))
                    WHERE l.RESULT_DATE IS NOT NULL
                      AND lm.measurement_concept_id IS NOT NULL
                    """
                )
                connection.commit()

                connection.exec_driver_sql(
                    f"""
                    WITH expanded AS (
                      SELECT
                        LTRIM(RTRIM(CONVERT(nvarchar(255), v.VITALID))) AS source_record_id,
                        x.source_field,
                        x.field_order
                      FROM {s('PCORnet_VITAL')} v
                      CROSS APPLY (VALUES
                        ('HT', 1, v.HT),
                        ('WT', 2, v.WT),
                        ('SYSTOLIC', 3, v.SYSTOLIC),
                        ('DIASTOLIC', 4, v.DIASTOLIC),
                        ('ORIGINAL_BMI', 5, v.ORIGINAL_BMI)
                      ) x(source_field, field_order, source_value)
                      WHERE x.source_value IS NOT NULL
                        AND v.MEASURE_DATE IS NOT NULL
                    )
                    INSERT INTO {t(XWALK_TABLE)} (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        {lab_measurement_rows}
                        + ROW_NUMBER() OVER (ORDER BY source_record_id, field_order),
                        'VITAL',
                        source_record_id,
                        source_field,
                        NULL
                    FROM expanded
                    """
                )
                connection.commit()

                connection.exec_driver_sql(
                    f"""
                    INSERT INTO {t(XWALK_TABLE)} (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        {lab_measurement_rows + vital_measurement_rows}
                        + ROW_NUMBER() OVER (ORDER BY r.route_id),
                        'PROCEDURES',
                        r.source_procedure_id,
                        NULL,
                        r.route_id
                    FROM {t(PROCEDURE_ROUTE_TABLE)} r
                    WHERE r.target_domain = 'Measurement'
                      AND r.px_date IS NOT NULL
                    """
                )
                connection.commit()

                lineage_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}",
                )
                if lineage_rows != expected_rows:
                    raise RuntimeError(
                        "Measurement lineage reconciliation failed: "
                        f"expected={expected_rows:,}, actual={lineage_rows:,}"
                    )

                lab_datetime = _safe_datetime_sql("l.RESULT_DATE", "l.RESULT_TIME")
                vital_datetime = _safe_datetime_sql("e.MEASURE_DATE", "e.MEASURE_TIME")

                unit_cte = f"""
                WITH unit_candidates AS (
                  SELECT
                    c.concept_code COLLATE Latin1_General_100_BIN2 AS concept_code,
                    c.concept_id,
                    COUNT_BIG(*) OVER (
                      PARTITION BY c.concept_code COLLATE Latin1_General_100_BIN2
                    ) AS candidate_count
                  FROM {t('concept')} c
                  WHERE c.vocabulary_id = 'UCUM'
                    AND c.domain_id = 'Unit'
                    AND c.standard_concept = 'S'
                    AND c.invalid_reason IS NULL
                ),
                unit_map AS (
                  SELECT
                    concept_code,
                    MAX(CASE WHEN candidate_count = 1 THEN concept_id ELSE NULL END)
                      AS unit_concept_id
                  FROM unit_candidates
                  GROUP BY concept_code
                )
                """

                connection.exec_driver_sql(
                    lab_map_cte
                    + ", "
                    + unit_cte.replace("WITH ", "", 1)
                    + f"""
                    INSERT INTO {t('measurement')} (
                        measurement_id, person_id, measurement_concept_id,
                        measurement_date, measurement_datetime, measurement_time,
                        measurement_type_concept_id, operator_concept_id,
                        value_as_number, value_as_concept_id, unit_concept_id,
                        range_low, range_high, provider_id, visit_occurrence_id,
                        visit_detail_id, measurement_source_value,
                        measurement_source_concept_id, unit_source_value,
                        unit_source_concept_id, value_source_value,
                        measurement_event_id, meas_event_field_concept_id
                    )
                    SELECT
                        x.measurement_id,
                        p.person_id,
                        lm.measurement_concept_id,
                        CAST(l.RESULT_DATE AS date),
                        {lab_datetime},
                        NULL,
                        0,
                        0,
                        TRY_CONVERT(float, l.RESULT_NUM),
                        0,
                        COALESCE(um.unit_concept_id, 0),
                        TRY_CONVERT(float, l.NORM_RANGE_LOW),
                        TRY_CONVERT(float, l.NORM_RANGE_HIGH),
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LTRIM(RTRIM(CONVERT(nvarchar(255), l.LAB_LOINC))),
                        lm.source_concept_id,
                        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), l.RESULT_UNIT))), ''),
                        COALESCE(um.unit_concept_id, 0),
                        COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), l.RAW_RESULT))), ''),
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), l.RESULT_QUAL))), '')
                        ),
                        NULL,
                        0
                    FROM {s('PCORnet_LAB_RESULT_CM')} l
                    JOIN lab_map lm
                      ON lm.loinc = LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC)))
                    JOIN {t(XWALK_TABLE)} x
                      ON x.source_family = 'LAB_RESULT_CM'
                     AND x.source_record_id = LTRIM(RTRIM(CONVERT(nvarchar(255), l.LAB_RESULT_CM_ID)))
                    JOIN {t('person')} p
                      ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), l.PATID)))
                    LEFT JOIN {t(VISIT_XWALK_TABLE)} vx
                      ON vx.encounterid = LTRIM(RTRIM(CONVERT(nvarchar(255), l.ENCOUNTERID)))
                    LEFT JOIN unit_map um
                      ON um.concept_code =
                         LTRIM(RTRIM(CONVERT(nvarchar(50), l.RESULT_UNIT)))
                         COLLATE Latin1_General_100_BIN2
                    WHERE l.RESULT_DATE IS NOT NULL
                      AND lm.measurement_concept_id IS NOT NULL
                    """
                )
                connection.commit()

                vital_values = []
                for field, (concept_id, unit_id, unit_code) in vital_specs.items():
                    safe_unit = unit_code.replace("'", "''")
                    vital_values.append(
                        f"('{field}', v.{field}, {concept_id}, {unit_id}, '{safe_unit}')"
                    )
                vital_values_sql = ",\n                        ".join(vital_values)

                connection.exec_driver_sql(
                    f"""
                    WITH expanded AS (
                      SELECT
                        v.VITALID,
                        v.PATID,
                        v.ENCOUNTERID,
                        v.MEASURE_DATE,
                        v.MEASURE_TIME,
                        x.source_field,
                        x.source_value,
                        x.measurement_concept_id,
                        x.unit_concept_id,
                        x.unit_source_value
                      FROM {s('PCORnet_VITAL')} v
                      CROSS APPLY (VALUES
                        {vital_values_sql}
                      ) x(
                        source_field,
                        source_value,
                        measurement_concept_id,
                        unit_concept_id,
                        unit_source_value
                      )
                      WHERE x.source_value IS NOT NULL
                        AND v.MEASURE_DATE IS NOT NULL
                    )
                    INSERT INTO {t('measurement')} (
                        measurement_id, person_id, measurement_concept_id,
                        measurement_date, measurement_datetime, measurement_time,
                        measurement_type_concept_id, operator_concept_id,
                        value_as_number, value_as_concept_id, unit_concept_id,
                        range_low, range_high, provider_id, visit_occurrence_id,
                        visit_detail_id, measurement_source_value,
                        measurement_source_concept_id, unit_source_value,
                        unit_source_concept_id, value_source_value,
                        measurement_event_id, meas_event_field_concept_id
                    )
                    SELECT
                        xw.measurement_id,
                        p.person_id,
                        e.measurement_concept_id,
                        CAST(e.MEASURE_DATE AS date),
                        {vital_datetime},
                        NULL,
                        0,
                        0,
                        TRY_CONVERT(float, e.source_value),
                        0,
                        e.unit_concept_id,
                        NULL,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        e.source_field,
                        0,
                        e.unit_source_value,
                        e.unit_concept_id,
                        CONVERT(nvarchar(255), e.source_value),
                        NULL,
                        0
                    FROM expanded e
                    JOIN {t(XWALK_TABLE)} xw
                      ON xw.source_family = 'VITAL'
                     AND xw.source_record_id = LTRIM(RTRIM(CONVERT(nvarchar(255), e.VITALID)))
                     AND xw.source_field = e.source_field
                    JOIN {t('person')} p
                      ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), e.PATID)))
                    LEFT JOIN {t(VISIT_XWALK_TABLE)} vx
                      ON vx.encounterid = LTRIM(RTRIM(CONVERT(nvarchar(255), e.ENCOUNTERID)))
                    """
                )
                connection.commit()

                connection.exec_driver_sql(
                    f"""
                    INSERT INTO {t('measurement')} (
                        measurement_id, person_id, measurement_concept_id,
                        measurement_date, measurement_datetime, measurement_time,
                        measurement_type_concept_id, operator_concept_id,
                        value_as_number, value_as_concept_id, unit_concept_id,
                        range_low, range_high, provider_id, visit_occurrence_id,
                        visit_detail_id, measurement_source_value,
                        measurement_source_concept_id, unit_source_value,
                        unit_source_concept_id, value_source_value,
                        measurement_event_id, meas_event_field_concept_id
                    )
                    SELECT
                        x.measurement_id,
                        p.person_id,
                        COALESCE(r.target_concept_id, 0),
                        r.px_date,
                        CAST(r.px_date AS datetime2(7)),
                        NULL,
                        0,
                        0,
                        NULL,
                        0,
                        0,
                        NULL,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        r.px,
                        COALESCE(r.source_concept_id, 0),
                        NULL,
                        0,
                        NULL,
                        NULL,
                        0
                    FROM {t(PROCEDURE_ROUTE_TABLE)} r
                    JOIN {t(XWALK_TABLE)} x
                      ON x.source_family = 'PROCEDURES'
                     AND x.source_route_id = r.route_id
                    JOIN {t('person')} p
                      ON p.person_source_value = r.patid
                    LEFT JOIN {t(VISIT_XWALK_TABLE)} vx
                      ON vx.encounterid = r.encounterid
                    WHERE r.target_domain = 'Measurement'
                      AND r.px_date IS NOT NULL
                    """
                )
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {t('measurement')}",
            )
            lineage_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}",
            )
            if target_rows != expected_rows or lineage_rows != expected_rows:
                raise RuntimeError(
                    "Measurement reconciliation failed: "
                    f"expected={expected_rows:,}, target={target_rows:,}, "
                    f"lineage={lineage_rows:,}"
                )

            target_concept_zero_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM {t('measurement')} "
                "WHERE measurement_concept_id = 0",
            )
            lab_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('measurement')} m
                JOIN {t(XWALK_TABLE)} x ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'LAB_RESULT_CM'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            vital_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('measurement')} m
                JOIN {t(XWALK_TABLE)} x ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'VITAL'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            procedure_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('measurement')} m
                JOIN {t(XWALK_TABLE)} x ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'PROCEDURES'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            lab_unit_concept_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('measurement')} m
                JOIN {t(XWALK_TABLE)} x ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'LAB_RESULT_CM'
                  AND m.unit_concept_id = 0
                """,
            )

            vital_implausibility = dict(
                connection.execute(
                    text(
                        f"""
                        SELECT
                          SUM(CASE WHEN HT IS NOT NULL AND (HT <= 0 OR HT > 100)
                                   THEN 1 ELSE 0 END) AS ht_flag,
                          SUM(CASE WHEN WT IS NOT NULL AND (WT <= 0 OR WT > 1500)
                                   THEN 1 ELSE 0 END) AS wt_flag,
                          SUM(CASE WHEN SYSTOLIC IS NOT NULL AND (SYSTOLIC <= 0 OR SYSTOLIC > 300)
                                   THEN 1 ELSE 0 END) AS systolic_flag,
                          SUM(CASE WHEN DIASTOLIC IS NOT NULL AND (DIASTOLIC <= 0 OR DIASTOLIC > 200)
                                   THEN 1 ELSE 0 END) AS diastolic_flag,
                          SUM(CASE WHEN ORIGINAL_BMI IS NOT NULL AND (ORIGINAL_BMI <= 0 OR ORIGINAL_BMI > 150)
                                   THEN 1 ELSE 0 END) AS bmi_flag
                        FROM {s('PCORnet_VITAL')}
                        """
                    )
                ).mappings().one()
            )
    finally:
        engine.dispose()

    result = MeasurementTransformResult(
        lab_source_rows=lab_source_rows,
        lab_measurement_rows=lab_measurement_rows,
        lab_direct_standard_rows=lab_direct_standard_rows,
        lab_mapped_rows=lab_mapped_rows,
        lab_observation_domain_rows=lab_observation_domain_rows,
        vital_source_rows=vital_source_rows,
        vital_measurement_rows=vital_measurement_rows,
        procedure_measurement_rows=procedure_measurement_rows,
        expected_rows=expected_rows,
        target_rows=target_rows,
        lineage_rows=lineage_rows,
        lab_visit_linked_rows=lab_visit_linked_rows,
        vital_visit_linked_rows=vital_visit_linked_rows,
        procedure_visit_linked_rows=procedure_visit_linked_rows,
        lab_unit_concept_zero_rows=lab_unit_concept_zero_rows,
        target_concept_zero_rows=target_concept_zero_rows,
        status=status,
        audit_path=audit_path,
    )

    payload = asdict(result)
    payload.update(
        {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "stage": "measurement",
            "lab_route_counts": {str(k): int(v) for k, v in lab_route_counts.items()},
            "lab_excluded_missing_result_date": lab_missing_result_date,
            "vital_excluded_missing_measure_date_rows": vital_missing_measure_date,
            "procedure_excluded_missing_px_date": procedure_missing_px_date,
            "vital_implausibility_flags_retained": vital_implausibility,
            "resolved_vital_specs": {
                field: {
                    "measurement_concept_id": values[0],
                    "unit_concept_id": values[1],
                    "unit_source_value": values[2],
                }
                for field, values in vital_specs.items()
            },
            "policies": {
                "required_dates": (
                    "Exclude source events missing RESULT_DATE, MEASURE_DATE, or PX_DATE "
                    "from the corresponding Measurement materialization and quantify them."
                ),
                "lab_mapping": (
                    "Use a unique exact LOINC source concept; direct active Standard "
                    "Measurement concepts map to themselves; otherwise follow active Maps to "
                    "only when exactly one Standard Measurement target exists. Ambiguous or "
                    "unresolved LAB events remain Measurement with concept_id=0."
                ),
                "lab_cross_domain": (
                    "Active Standard LOINCs in another domain are not forced into Measurement."
                ),
                "lab_units": (
                    "Exact case-sensitive unique active Standard UCUM Unit mapping; otherwise "
                    "unit_concept_id=0 with unit_source_value preserved."
                ),
                "vital_concepts": (
                    "Resolve fixed PCORnet VITAL field semantics through exact active Standard "
                    "LOINC and UCUM codes at run time rather than dataset-specific concept ids."
                ),
                "vital_values": (
                    "Retain all non-null quantitative values; plausibility flags are audit-only."
                ),
                "qualitative_values": (
                    "Do not guess RESULT_QUAL concepts. Preserve RAW_RESULT, then RESULT_QUAL, "
                    "in value_source_value."
                ),
                "procedure_measurements": (
                    "Respect the canonical procedure route ledger; do not invent absent result "
                    "values, units, or type provenance."
                ),
                "lineage_schema": (
                    "PCORnet staging tables use source_schema; OMOP tables and ETL route/xwalk "
                    "ledgers use target_schema."
                ),
            },
        }
    )

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return result
