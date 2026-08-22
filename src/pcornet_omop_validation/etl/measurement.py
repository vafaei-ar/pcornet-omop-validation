from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_measurement_xwalk"
PROCEDURE_ROUTE_TABLE = "etl_procedure_event_route"

# Validated against Athena vocabulary v20260227.
VITAL_CONCEPTS = {
    "HT": 3036277,            # LOINC 8302-2 Body height
    "WT": 3025315,            # LOINC 29463-7 Body weight
    "SYSTOLIC": 3004249,      # LOINC 8480-6
    "DIASTOLIC": 3012888,     # LOINC 8462-4
    "ORIGINAL_BMI": 3038553,  # LOINC 39156-5
}

VITAL_UNITS = {
    "HT": (9327, "[in_i]"),
    # No active standard [lb_av] Unit concept was found in this vocabulary.
    "WT": (0, "[lb_av]"),
    "SYSTOLIC": (8876, "mm[Hg]"),
    "DIASTOLIC": (8876, "mm[Hg]"),
    "ORIGINAL_BMI": (9531, "kg/m2"),
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


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_LAB_RESULT_CM"),
        (source_schema, "PCORnet_VITAL"),
        (source_schema, PROCEDURE_ROUTE_TABLE),
        (source_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "person"),
        (target_schema, "measurement"),
        (target_schema, "concept"),
        (target_schema, "concept_relationship"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(
                f"Required table [{schema}].[{table}] does not exist"
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
    """
    Resolve LAB_LOINC while respecting OMOP domain.

    Active standard Measurement LOINCs map directly.
    Nonstandard/invalid source concepts may follow active Maps to relationships
    to standard Measurement concepts.
    Active standard concepts in another domain are not forced to Measurement.
    """
    return f"""
    WITH lab_codes AS (
      SELECT DISTINCT
        LTRIM(RTRIM(CONVERT(nvarchar(100), LAB_LOINC))) AS loinc
      FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]
    ),
    source_concepts AS (
      SELECT
        lc.loinc,
        c.concept_id AS source_concept_id,
        c.domain_id AS source_domain,
        c.standard_concept AS source_standard_concept,
        c.invalid_reason AS source_invalid_reason
      FROM lab_codes lc
      LEFT JOIN [{target_schema}].[concept] c
        ON c.vocabulary_id = 'LOINC'
       AND c.concept_code = lc.loinc
    ),
    mapped_targets AS (
      SELECT DISTINCT
        sc.loinc,
        sc.source_concept_id,
        tgt.concept_id AS target_concept_id
      FROM source_concepts sc
      JOIN [{target_schema}].[concept_relationship] cr
        ON cr.concept_id_1 = sc.source_concept_id
       AND cr.relationship_id = 'Maps to'
       AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
      JOIN [{target_schema}].[concept] tgt
        ON tgt.concept_id = cr.concept_id_2
       AND tgt.standard_concept = 'S'
       AND tgt.invalid_reason IS NULL
       AND tgt.domain_id = 'Measurement'
      WHERE NOT (
        sc.source_invalid_reason IS NULL
        AND COALESCE(sc.source_standard_concept, '') = 'S'
      )
    ),
    mapped_counts AS (
      SELECT loinc, COUNT(DISTINCT target_concept_id) AS n_targets
      FROM mapped_targets
      GROUP BY loinc
    ),
    lab_map AS (
      SELECT
        sc.loinc,
        COALESCE(sc.source_concept_id, 0) AS source_concept_id,
        CASE
          WHEN sc.source_invalid_reason IS NULL
           AND sc.source_standard_concept = 'S'
           AND sc.source_domain = 'Measurement'
            THEN sc.source_concept_id
          WHEN mc.n_targets = 1
            THEN mt.target_concept_id
          ELSE NULL
        END AS measurement_concept_id,
        CASE
          WHEN sc.source_invalid_reason IS NULL
           AND sc.source_standard_concept = 'S'
           AND sc.source_domain = 'Measurement'
            THEN 'direct_standard_measurement'
          WHEN sc.source_invalid_reason IS NULL
           AND sc.source_standard_concept = 'S'
           AND sc.source_domain <> 'Measurement'
            THEN 'standard_other_domain'
          WHEN mc.n_targets = 1
            THEN 'maps_to_standard_measurement'
          WHEN mc.n_targets > 1
            THEN 'multiple_measurement_targets'
          WHEN sc.source_concept_id IS NULL
            THEN 'source_concept_not_found'
          ELSE 'no_active_measurement_target'
        END AS route_status
      FROM source_concepts sc
      LEFT JOIN mapped_counts mc
        ON mc.loinc = sc.loinc
      LEFT JOIN mapped_targets mt
        ON mt.loinc = sc.loinc
       AND mc.n_targets = 1
    )
    """


def _validate_vital_concepts(connection, target_schema: str) -> None:
    expected = {
        3036277: ("Measurement", "LOINC", "8302-2"),
        3025315: ("Measurement", "LOINC", "29463-7"),
        3004249: ("Measurement", "LOINC", "8480-6"),
        3012888: ("Measurement", "LOINC", "8462-4"),
        3038553: ("Measurement", "LOINC", "39156-5"),
        9327: ("Unit", "UCUM", "[in_i]"),
        8876: ("Unit", "UCUM", "mm[Hg]"),
        9531: ("Unit", "UCUM", "kg/m2"),
        8555: ("Unit", "UCUM", "s"),
        8505: ("Unit", "UCUM", "h"),
    }

    ids = ",".join(str(x) for x in sorted(expected))
    rows = connection.execute(
        text(
            f"""
            SELECT concept_id, domain_id, vocabulary_id, concept_code,
                   standard_concept, invalid_reason
            FROM [{target_schema}].[concept]
            WHERE concept_id IN ({ids})
            """
        )
    ).fetchall()

    observed = {int(r[0]): r for r in rows}

    for concept_id, (domain, vocabulary, code) in expected.items():
        row = observed.get(concept_id)
        if row is None:
            raise RuntimeError(
                f"Required validated concept {concept_id} is absent"
            )
        if (
            row[1] != domain
            or row[2] != vocabulary
            or row[3] != code
            or row[4] != "S"
            or row[5] is not None
        ):
            raise RuntimeError(
                f"Concept {concept_id} no longer matches validated semantics: "
                f"{tuple(row)}"
            )


def transform_measurement(config: EtlConfig) -> MeasurementTransformResult:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "Validated Measurement ETL requires "
            "policies.unmapped_standard_concept=concept_zero"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "measurement_transform.json"

    engine = make_engine(config)

    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            _validate_vital_concepts(connection, target_schema)

            lab_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{source_schema}].[PCORnet_LAB_RESULT_CM]",
            )
            vital_source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{source_schema}].[PCORnet_VITAL]",
            )

            lab_map_cte = _lab_mapping_cte(
                source_schema, target_schema
            )

            lab_route_counts = dict(
                connection.execute(
                    text(
                        lab_map_cte
                        + f"""
                        SELECT lm.route_status, COUNT_BIG(*) AS n
                        FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                        JOIN lab_map lm
                          ON lm.loinc =
                             LTRIM(RTRIM(CONVERT(nvarchar(100), l.LAB_LOINC)))
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

            unexpected_lab_rows = sum(
                int(v)
                for k, v in lab_route_counts.items()
                if k
                not in {
                    "direct_standard_measurement",
                    "maps_to_standard_measurement",
                    "standard_other_domain",
                }
            )
            if unexpected_lab_rows:
                raise RuntimeError(
                    "LAB routing produced unresolved or ambiguous rows: "
                    f"{lab_route_counts}"
                )

            lab_measurement_rows = (
                lab_direct_standard_rows + lab_mapped_rows
            )

            vital_measurement_rows = _scalar(
                connection,
                f"""
                SELECT
                    SUM(
                        CASE WHEN HT IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN WT IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN SYSTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN DIASTOLIC IS NOT NULL THEN 1 ELSE 0 END +
                        CASE WHEN ORIGINAL_BMI IS NOT NULL THEN 1 ELSE 0 END
                    )
                FROM [{source_schema}].[PCORnet_VITAL]
                """,
            )

            procedure_measurement_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[{PROCEDURE_ROUTE_TABLE}]
                WHERE target_domain = 'Measurement'
                """,
            )

            expected_rows = (
                lab_measurement_rows
                + vital_measurement_rows
                + procedure_measurement_rows
            )

            invalid_procedure_targets = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[{PROCEDURE_ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_domain = 'Measurement'
                  AND r.target_concept_id <> 0
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

            xwalk_exists = table_exists(
                connection, source_schema, XWALK_TABLE
            )
            existing_target = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{target_schema}].[measurement]",
            )

            if xwalk_exists:
                lineage_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) "
                    f"FROM [{source_schema}].[{XWALK_TABLE}]",
                )
                if lineage_rows != expected_rows:
                    raise RuntimeError(
                        f"Existing [{source_schema}].[{XWALK_TABLE}] has "
                        f"{lineage_rows:,} rows; expected {expected_rows:,}"
                    )
            else:
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE [{source_schema}].[{XWALK_TABLE}] (
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

                # LAB: one eligible Measurement per source record.
                connection.exec_driver_sql(
                    lab_map_cte
                    + f"""
                    INSERT INTO [{source_schema}].[{XWALK_TABLE}] (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        ROW_NUMBER() OVER (
                            ORDER BY
                              LTRIM(RTRIM(CONVERT(
                                nvarchar(255), l.LAB_RESULT_CM_ID
                              )))
                        ),
                        'LAB_RESULT_CM',
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), l.LAB_RESULT_CM_ID
                        ))),
                        NULL,
                        NULL
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN lab_map lm
                      ON lm.loinc =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(100), l.LAB_LOINC
                         )))
                    WHERE lm.measurement_concept_id IS NOT NULL
                    """
                )
                connection.commit()

                # VITAL: one Measurement per non-null quantitative field.
                connection.exec_driver_sql(
                    f"""
                    WITH expanded AS (
                      SELECT
                        LTRIM(RTRIM(CONVERT(
                          nvarchar(255), v.VITALID
                        ))) AS source_record_id,
                        x.source_field,
                        x.field_order
                      FROM [{source_schema}].[PCORnet_VITAL] v
                      CROSS APPLY (VALUES
                        ('HT', 1, v.HT),
                        ('WT', 2, v.WT),
                        ('SYSTOLIC', 3, v.SYSTOLIC),
                        ('DIASTOLIC', 4, v.DIASTOLIC),
                        ('ORIGINAL_BMI', 5, v.ORIGINAL_BMI)
                      ) x(source_field, field_order, source_value)
                      WHERE x.source_value IS NOT NULL
                    )
                    INSERT INTO [{source_schema}].[{XWALK_TABLE}] (
                        measurement_id,
                        source_family,
                        source_record_id,
                        source_field,
                        source_route_id
                    )
                    SELECT
                        {lab_measurement_rows}
                        + ROW_NUMBER() OVER (
                            ORDER BY source_record_id, field_order
                          ),
                        'VITAL',
                        source_record_id,
                        source_field,
                        NULL
                    FROM expanded
                    """
                )
                connection.commit()

                # Procedure-derived Measurement routes.
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO [{source_schema}].[{XWALK_TABLE}] (
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
                    FROM [{source_schema}].[{PROCEDURE_ROUTE_TABLE}] r
                    WHERE r.target_domain = 'Measurement'
                    """
                )
                connection.commit()

                lineage_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) "
                    f"FROM [{source_schema}].[{XWALK_TABLE}]",
                )
                if lineage_rows != expected_rows:
                    raise RuntimeError(
                        "Measurement lineage reconciliation failed: "
                        f"expected={expected_rows:,}, "
                        f"actual={lineage_rows:,}"
                    )

            if existing_target:
                if existing_target != expected_rows:
                    raise RuntimeError(
                        f"Target [{target_schema}].[measurement] already "
                        f"contains {existing_target:,} rows; expected "
                        f"{expected_rows:,}. Refusing to append."
                    )
                status = "already_loaded_matched"
            else:
                lab_datetime = _safe_datetime_sql(
                    "l.RESULT_DATE", "l.RESULT_TIME"
                )
                vital_datetime = _safe_datetime_sql(
                    "e.MEASURE_DATE", "e.MEASURE_TIME"
                )

                # UCUM codes are case-sensitive. Force binary collation so
                # SQL Server cannot create false U/u or similar matches.
                # Only an exact, unique, active Standard Unit concept maps.
                lab_unit_cte = f"""
                WITH unit_candidates AS (
                  SELECT
                    c.concept_code COLLATE Latin1_General_100_BIN2
                      AS concept_code,
                    c.concept_id,
                    COUNT_BIG(*) OVER (
                      PARTITION BY
                        c.concept_code COLLATE Latin1_General_100_BIN2
                    ) AS candidate_count
                  FROM [{target_schema}].[concept] c
                  WHERE c.vocabulary_id = 'UCUM'
                    AND c.domain_id = 'Unit'
                    AND c.standard_concept = 'S'
                    AND c.invalid_reason IS NULL
                ),
                unit_map AS (
                  SELECT
                    concept_code,
                    MAX(
                      CASE
                        WHEN candidate_count = 1 THEN concept_id
                        ELSE NULL
                      END
                    ) AS unit_concept_id
                  FROM unit_candidates
                  GROUP BY concept_code
                )
                """

                # LAB_RESULT_CM
                connection.exec_driver_sql(
                    lab_map_cte
                    + ", "
                    + lab_unit_cte.replace("WITH ", "", 1)
                    + f"""
                    INSERT INTO [{target_schema}].[measurement] (
                        measurement_id,
                        person_id,
                        measurement_concept_id,
                        measurement_date,
                        measurement_datetime,
                        measurement_time,
                        measurement_type_concept_id,
                        operator_concept_id,
                        value_as_number,
                        value_as_concept_id,
                        unit_concept_id,
                        range_low,
                        range_high,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        measurement_source_value,
                        measurement_source_concept_id,
                        unit_source_value,
                        unit_source_concept_id,
                        value_source_value,
                        measurement_event_id,
                        meas_event_field_concept_id
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
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), l.LAB_LOINC
                        ))),
                        lm.source_concept_id,
                        NULLIF(LTRIM(RTRIM(CONVERT(
                            nvarchar(50), l.RESULT_UNIT
                        ))), ''),
                        COALESCE(um.unit_concept_id, 0),
                        COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(
                              nvarchar(255), l.RAW_RESULT
                            ))), ''),
                            NULLIF(LTRIM(RTRIM(CONVERT(
                              nvarchar(255), l.RESULT_QUAL
                            ))), '')
                        ),
                        NULL,
                        0
                    FROM [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                    JOIN lab_map lm
                      ON lm.loinc =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(100), l.LAB_LOINC
                         )))
                    JOIN [{source_schema}].[{XWALK_TABLE}] x
                      ON x.source_family = 'LAB_RESULT_CM'
                     AND x.source_record_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), l.LAB_RESULT_CM_ID
                         )))
                    JOIN [{target_schema}].[person] p
                      ON p.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), l.PATID
                         )))
                    LEFT JOIN [{source_schema}].[etl_visit_occurrence_xwalk] vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), l.ENCOUNTERID
                         )))
                    LEFT JOIN unit_map um
                      ON um.concept_code =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(50), l.RESULT_UNIT
                         ))) COLLATE Latin1_General_100_BIN2
                    WHERE lm.measurement_concept_id IS NOT NULL
                    """
                )
                connection.commit()

                # VITAL expansion.
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
                      FROM [{source_schema}].[PCORnet_VITAL] v
                      CROSS APPLY (VALUES
                        ('HT', v.HT, 3036277, 9327, '[in_i]'),
                        ('WT', v.WT, 3025315, 0, '[lb_av]'),
                        ('SYSTOLIC', v.SYSTOLIC, 3004249, 8876, 'mm[Hg]'),
                        ('DIASTOLIC', v.DIASTOLIC, 3012888, 8876, 'mm[Hg]'),
                        ('ORIGINAL_BMI', v.ORIGINAL_BMI, 3038553, 9531, 'kg/m2')
                      ) x(
                        source_field,
                        source_value,
                        measurement_concept_id,
                        unit_concept_id,
                        unit_source_value
                      )
                      WHERE x.source_value IS NOT NULL
                    )
                    INSERT INTO [{target_schema}].[measurement] (
                        measurement_id,
                        person_id,
                        measurement_concept_id,
                        measurement_date,
                        measurement_datetime,
                        measurement_time,
                        measurement_type_concept_id,
                        operator_concept_id,
                        value_as_number,
                        value_as_concept_id,
                        unit_concept_id,
                        range_low,
                        range_high,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        measurement_source_value,
                        measurement_source_concept_id,
                        unit_source_value,
                        unit_source_concept_id,
                        value_source_value,
                        measurement_event_id,
                        meas_event_field_concept_id
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
                    JOIN [{source_schema}].[{XWALK_TABLE}] xw
                      ON xw.source_family = 'VITAL'
                     AND xw.source_record_id =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), e.VITALID
                         )))
                     AND xw.source_field = e.source_field
                    JOIN [{target_schema}].[person] p
                      ON p.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), e.PATID
                         )))
                    LEFT JOIN [{source_schema}].[etl_visit_occurrence_xwalk] vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                           nvarchar(255), e.ENCOUNTERID
                         )))
                    """
                )
                connection.commit()

                # Procedure-derived Measurement routes. No result value or
                # unit is invented when the source procedure supplies none.
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO [{target_schema}].[measurement] (
                        measurement_id,
                        person_id,
                        measurement_concept_id,
                        measurement_date,
                        measurement_datetime,
                        measurement_time,
                        measurement_type_concept_id,
                        operator_concept_id,
                        value_as_number,
                        value_as_concept_id,
                        unit_concept_id,
                        range_low,
                        range_high,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        measurement_source_value,
                        measurement_source_concept_id,
                        unit_source_value,
                        unit_source_concept_id,
                        value_source_value,
                        measurement_event_id,
                        meas_event_field_concept_id
                    )
                    SELECT
                        x.measurement_id,
                        p.person_id,
                        r.target_concept_id,
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
                        r.source_concept_id,
                        NULL,
                        0,
                        NULL,
                        NULL,
                        0
                    FROM [{source_schema}].[{PROCEDURE_ROUTE_TABLE}] r
                    JOIN [{source_schema}].[{XWALK_TABLE}] x
                      ON x.source_family = 'PROCEDURES'
                     AND x.source_route_id = r.route_id
                    JOIN [{target_schema}].[person] p
                      ON p.person_source_value = r.patid
                    LEFT JOIN [{source_schema}].[etl_visit_occurrence_xwalk] vx
                      ON vx.encounterid = r.encounterid
                    WHERE r.target_domain = 'Measurement'
                    """
                )
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{target_schema}].[measurement]",
            )
            lineage_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{source_schema}].[{XWALK_TABLE}]",
            )

            if target_rows != expected_rows or lineage_rows != expected_rows:
                raise RuntimeError(
                    "Measurement reconciliation failed: "
                    f"expected={expected_rows:,}, "
                    f"target={target_rows:,}, "
                    f"lineage={lineage_rows:,}"
                )

            target_concept_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement]
                WHERE measurement_concept_id = 0
                """,
            )

            lab_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'LAB_RESULT_CM'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            vital_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'VITAL'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            procedure_visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'PROCEDURES'
                  AND m.visit_occurrence_id IS NOT NULL
                """,
            )
            lab_unit_concept_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.measurement_id = m.measurement_id
                WHERE x.source_family = 'LAB_RESULT_CM'
                  AND m.unit_concept_id = 0
                """,
            )

            vital_implausibility = dict(
                connection.execute(
                    text(
                        f"""
                        SELECT
                          SUM(CASE WHEN HT IS NOT NULL
                                    AND (HT <= 0 OR HT > 100)
                                   THEN 1 ELSE 0 END) AS ht_flag,
                          SUM(CASE WHEN WT IS NOT NULL
                                    AND (WT <= 0 OR WT > 1500)
                                   THEN 1 ELSE 0 END) AS wt_flag,
                          SUM(CASE WHEN SYSTOLIC IS NOT NULL
                                    AND (SYSTOLIC <= 0 OR SYSTOLIC > 300)
                                   THEN 1 ELSE 0 END) AS systolic_flag,
                          SUM(CASE WHEN DIASTOLIC IS NOT NULL
                                    AND (DIASTOLIC <= 0 OR DIASTOLIC > 200)
                                   THEN 1 ELSE 0 END) AS diastolic_flag,
                          SUM(CASE WHEN ORIGINAL_BMI IS NOT NULL
                                    AND (ORIGINAL_BMI <= 0
                                         OR ORIGINAL_BMI > 150)
                                   THEN 1 ELSE 0 END) AS bmi_flag
                        FROM [{source_schema}].[PCORnet_VITAL]
                        """
                    )
                ).mappings().one()
            )

    finally:
        engine.dispose()

    payload = asdict(
        MeasurementTransformResult(
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
    )
    payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["stage"] = "measurement"
    payload["vital_implausibility_flags_retained"] = vital_implausibility
    payload["policies"] = {
        "lab_cross_domain": (
            "Active standard Observation-domain LOINCs are not forced "
            "into Measurement."
        ),
        "invalid_loinc": (
            "Follow active Maps to relationships to standard "
            "Measurement concepts."
        ),
        "vital_values": (
            "All non-null quantitative values are retained. "
            "Plausibility flags are audit-only."
        ),
        "lab_units": (
            "Exact case-sensitive active standard UCUM mapping when "
            "there is exactly one matching Unit concept; otherwise "
            "unit_concept_id=0 with unit_source_value preserved."
        ),
        "weight_unit": (
            "[lb_av] preserved as unit_source_value with "
            "unit_concept_id=0 because no validated active standard "
            "Unit concept was found in the loaded vocabulary."
        ),
        "qualitative_values": (
            "No RESULT_QUAL concept mapping is guessed. RAW_RESULT, "
            "then RESULT_QUAL, is preserved in value_source_value."
        ),
        "procedure_measurements": (
            "Procedure-domain routing is respected; absent result "
            "values and units are not invented."
        ),
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    return MeasurementTransformResult(
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
