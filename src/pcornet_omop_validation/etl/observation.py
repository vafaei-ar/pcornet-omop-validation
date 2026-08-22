from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_observation_xwalk"
OVERFLOW_TABLE = "etl_observation_text_overflow"

# Validated PCORnet-to-OMOP semantic mappings for categorical VITAL fields.
VITAL_OBSERVATION_CONCEPTS = {
    "SMOKING": 43054909,      # LOINC 72166-2 Tobacco smoking status
    "TOBACCO": 3039561,       # LOINC 39240-7 Tobacco use status CPHS
    "TOBACCO_TYPE": 42528924, # LOINC 82769-1 Smoked or non-smoked tobacco
}

# Exact standard answer concepts only.
SMOKING_VALUES = {
    "01": 45881517,
    "02": 45884037,
    "03": 45883458,
    "04": 45879404,
    "05": 45881518,
    "06": 45885135,
    "07": 45884038,
    "08": 45878118,
    "OT": 0,
}

TOBACCO_TYPE_VALUES = {
    "01": 42530793,
    "03": 42531020,
    "05": 42530756,
    "04": 0,
    "OT": 0,
}


@dataclass
class ObservationTransformResult:
    obs_clin_rows: int
    obs_gen_rows: int
    lab_rows: int
    procedure_rows: int
    vital_rows: int
    expected_rows: int
    target_rows: int
    lineage_rows: int
    concept_zero_rows: int
    vital_value_concept_zero_rows: int
    visit_linked_rows: int
    overflow_rows: int
    status: str
    audit_path: str


def _safe_datetime_sql(date_expr: str, time_expr: str) -> str:
    return f"""
    CASE
      WHEN {date_expr} IS NULL THEN NULL
      WHEN TRY_CONVERT(float, {time_expr}) IS NULL
        OR TRY_CONVERT(float, {time_expr}) < 0
        OR TRY_CONVERT(float, {time_expr}) >= 86400
        THEN CAST(CAST({date_expr} AS date) AS datetime2(7))
      ELSE DATEADD(
        MILLISECOND,
        CAST(
          ROUND(
            TRY_CONVERT(float, {time_expr}) * 1000.0,
            0
          ) AS bigint
        ),
        CAST(CAST({date_expr} AS date) AS datetime2(7))
      )
    END
    """


def _validated_schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _render_sql(sql: str, source_schema: str, target_schema: str) -> str:
    """Render legacy dbo-qualified SQL using configured source/target schemas.

    PCORnet staging tables are source objects. OMOP tables and ETL lineage/route
    ledgers are target objects. Keeping this classification in one place makes
    the transformation portable without changing its semantic rules.
    """
    return sql.replace(
        "dbo.PCORnet_",
        f"{source_schema}.PCORnet_",
    ).replace(
        "dbo.",
        f"{target_schema}.",
    )


def _column_max_chars(
    connection,
    table: str,
    column: str,
    target_schema: str,
) -> int:
    row = connection.execute(
        text("""
            SELECT
                TYPE_NAME(c.user_type_id) AS data_type,
                c.max_length
            FROM sys.columns c
            WHERE c.object_id = OBJECT_ID(:table)
              AND c.name = :column
        """),
        {"table": f"{target_schema}.{table}", "column": column},
    ).one()

    data_type = str(row[0]).lower()
    max_length = int(row[1])

    if max_length == -1:
        return 2_147_483_647
    if data_type in ("nvarchar", "nchar"):
        return max_length // 2
    return max_length


def _procedure_route_columns(connection, target_schema: str) -> tuple[str, str | None]:
    cols = {
        str(r[0]).lower(): str(r[0])
        for r in connection.execute(
            text(f"""
                SELECT c.name
                FROM sys.columns c
                WHERE c.object_id =
                      OBJECT_ID('{target_schema}.etl_procedure_event_route')
            """)
        )
    }

    id_candidates = (
        "source_procedure_id",
        "proceduresid",
        "procedureid",
        "source_record_id",
    )
    source_id = next(
        (cols[x] for x in id_candidates if x in cols),
        None,
    )
    if source_id is None:
        raise RuntimeError(
            "Could not identify source procedure ID column in "
            f"{target_schema}.etl_procedure_event_route"
        )

    source_concept_candidates = (
        "source_concept_id",
        "procedure_source_concept_id",
    )
    source_concept = next(
        (cols[x] for x in source_concept_candidates if x in cols),
        None,
    )

    return source_id, source_concept


def _validate_vital_concepts(connection, target_schema: str) -> None:
    expected = {
        43054909: ("Observation", "LOINC", "72166-2"),
        3039561: ("Observation", "LOINC", "39240-7"),
        42528924: ("Observation", "LOINC", "82769-1"),
        45881517: ("Meas Value", "LOINC", "LA18976-3"),
        45884037: ("Meas Value", "LOINC", "LA18977-1"),
        45883458: ("Meas Value", "LOINC", "LA15920-4"),
        45879404: ("Meas Value", "LOINC", "LA18978-9"),
        45881518: ("Meas Value", "LOINC", "LA18979-7"),
        45885135: ("Meas Value", "LOINC", "LA18980-5"),
        45884038: ("Meas Value", "LOINC", "LA18981-3"),
        45878118: ("Meas Value", "LOINC", "LA18982-1"),
        42530793: ("Meas Value", "LOINC", "LA26671-0"),
        42531020: ("Meas Value", "LOINC", "LA26673-6"),
        42530756: ("Meas Value", "LOINC", "LA26674-4"),
    }

    ids = ",".join(str(x) for x in expected)

    rows = connection.execute(
        text(f"""
            SELECT
                concept_id,
                domain_id,
                vocabulary_id,
                concept_code,
                standard_concept,
                invalid_reason
            FROM {target_schema}.concept
            WHERE concept_id IN ({ids})
        """)
    ).fetchall()

    got = {int(r[0]): r for r in rows}

    for cid, (domain, vocabulary, code) in expected.items():
        r = got.get(cid)
        if (
            r is None
            or r[1] != domain
            or r[2] != vocabulary
            or r[3] != code
            or r[4] != "S"
            or r[5] is not None
        ):
            raise RuntimeError(
                f"Validated concept {cid} does not match expected semantics"
            )


def transform_observation(config: EtlConfig) -> ObservationTransformResult:
    engine = make_engine(config)
    audit_path = config.audit_dir / "observation_transform.json"

    sql_cfg = config.raw["sqlserver"]
    source_schema = _validated_schema(
        sql_cfg.get("source_schema", "dbo"),
        "source_schema",
    )
    target_schema = _validated_schema(
        sql_cfg.get("target_schema", "dbo"),
        "target_schema",
    )

    def sql(statement: str):
        return text(_render_sql(statement, source_schema, target_schema))

    try:
        with engine.begin() as con:
            source_required = (
                "PCORnet_OBS_CLIN",
                "PCORnet_OBS_GEN",
                "PCORnet_LAB_RESULT_CM",
                "PCORnet_PROCEDURES",
                "PCORnet_VITAL",
            )
            target_required = (
                "observation",
                "person",
                "etl_visit_occurrence_xwalk",
                "etl_obs_clin_route",
                "etl_procedure_event_route",
                "concept",
            )

            for table in source_required:
                if not table_exists(con, source_schema, table):
                    raise RuntimeError(
                        f"Required table {source_schema}.{table} does not exist"
                    )
            for table in target_required:
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(
                        f"Required table {target_schema}.{table} does not exist"
                    )

            target_rows_pre = int(
                con.execute(
                    sql("SELECT COUNT_BIG(*) FROM dbo.observation")
                ).scalar_one()
            )
            if target_rows_pre != 0:
                raise RuntimeError(
                    f"{target_schema}.observation must be empty before this stage; "
                    f"found {target_rows_pre:,} rows"
                )

            if table_exists(con, target_schema, XWALK_TABLE):
                raise RuntimeError(
                    f"{target_schema}.{XWALK_TABLE} already exists"
                )
            if table_exists(con, target_schema, OVERFLOW_TABLE):
                raise RuntimeError(
                    f"{target_schema}.{OVERFLOW_TABLE} already exists"
                )

            _validate_vital_concepts(con, target_schema)

            procedure_source_id, procedure_source_concept = (
                _procedure_route_columns(con, target_schema)
            )
            procedure_source_concept_expr = (
                f"r.[{procedure_source_concept}]"
                if procedure_source_concept
                else "0"
            )

            # Derive denominators from source tables and route ledgers.
            counts: dict[str, int] = {}

            counts["OBS_CLIN"] = int(
                con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Observation'
                    """)
                ).scalar_one()
            )

            counts["OBS_GEN"] = int(
                con.execute(
                    sql("SELECT COUNT_BIG(*) FROM dbo.PCORnet_OBS_GEN")
                ).scalar_one()
            )

            counts["LAB_RESULT_CM"] = int(
                con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.PCORnet_LAB_RESULT_CM l
                        JOIN dbo.concept c
                          ON c.vocabulary_id = 'LOINC'
                         AND c.concept_code =
                             LTRIM(RTRIM(CONVERT(
                               nvarchar(255), l.LAB_LOINC
                             )))
                         AND c.standard_concept = 'S'
                         AND c.invalid_reason IS NULL
                         AND c.domain_id = 'Observation'
                    """)
                ).scalar_one()
            )

            counts["PROCEDURES"] = int(
                con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_procedure_event_route
                        WHERE target_domain = 'Observation'
                    """)
                ).scalar_one()
            )

            counts["VITAL"] = int(
                con.execute(
                    sql("""
                        SELECT
                            COUNT_BIG(SMOKING)
                          + COUNT_BIG(TOBACCO)
                          + COUNT_BIG(TOBACCO_TYPE)
                        FROM dbo.PCORnet_VITAL
                    """)
                ).scalar_one()
            )

            expected_total = sum(counts.values())

            expected_concept_zero = int(
                counts["OBS_GEN"]
                + con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_obs_clin_route
                        WHERE target_domain = 'Observation'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
                + con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_procedure_event_route
                        WHERE target_domain = 'Observation'
                          AND COALESCE(target_concept_id, 0) = 0
                    """)
                ).scalar_one()
            )

            expected_vital_value_zero = int(
                con.execute(
                    sql("""
                        SELECT
                            COALESCE(SUM(CASE
                                  WHEN SMOKING IS NOT NULL
                                   AND SMOKING NOT IN (
                                     '01','02','03','04','05','06','07','08'
                                   )
                                  THEN 1 ELSE 0 END), 0)
                          + COUNT_BIG(TOBACCO)
                          + COALESCE(SUM(CASE
                                  WHEN TOBACCO_TYPE IS NOT NULL
                                   AND TOBACCO_TYPE NOT IN ('01','03','05')
                                  THEN 1 ELSE 0 END), 0)
                        FROM dbo.PCORnet_VITAL
                    """)
                ).scalar_one()
            )

            # Actual OMOP string limits from the installed DDL.
            source_max = _column_max_chars(
                con,
                "observation",
                "observation_source_value",
                target_schema,
            )
            value_max = _column_max_chars(
                con,
                "observation",
                "value_source_value",
                target_schema,
            )
            string_max = _column_max_chars(
                con,
                "observation",
                "value_as_string",
                target_schema,
            )
            unit_max = _column_max_chars(
                con,
                "observation",
                "unit_source_value",
                target_schema,
            )

            con.exec_driver_sql(
                _render_sql(
                    f"""
                    CREATE TABLE dbo.{XWALK_TABLE} (
                        observation_id bigint NOT NULL PRIMARY KEY,
                        source_family varchar(32) NOT NULL,
                        source_record_id nvarchar(255) NOT NULL,
                        source_field varchar(32) NULL,
                        source_route_id bigint NULL
                    )
                    """,
                    source_schema,
                    target_schema,
                )
            )

            con.exec_driver_sql(
                _render_sql(
                    f"""
                    CREATE TABLE dbo.{OVERFLOW_TABLE} (
                        observation_id bigint NOT NULL PRIMARY KEY,
                        source_family varchar(32) NOT NULL,
                        source_record_id nvarchar(255) NOT NULL,
                        source_field varchar(32) NULL,
                        full_source_value nvarchar(max) NOT NULL,
                        source_length int NOT NULL
                    )
                    """,
                    source_schema,
                    target_schema,
                )
            )

            # XWALK: deterministic contiguous IDs by source family.
            con.execute(sql(f"""
                INSERT INTO dbo.{XWALK_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    source_route_id
                )
                SELECT
                    ROW_NUMBER() OVER (ORDER BY source_obsclin_id),
                    'OBS_CLIN',
                    source_obsclin_id,
                    NULL,
                    route_id
                FROM dbo.etl_obs_clin_route
                WHERE target_domain = 'Observation'
            """))

            obs_gen_offset = counts["OBS_CLIN"]

            con.execute(sql(f"""
                INSERT INTO dbo.{XWALK_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    source_route_id
                )
                SELECT
                    {obs_gen_offset}
                    + ROW_NUMBER() OVER (
                        ORDER BY LTRIM(RTRIM(CONVERT(
                          nvarchar(255), OBSGENID
                        )))
                    ),
                    'OBS_GEN',
                    LTRIM(RTRIM(CONVERT(nvarchar(255), OBSGENID))),
                    NULL,
                    NULL
                FROM dbo.PCORnet_OBS_GEN
            """))

            lab_offset = obs_gen_offset + counts["OBS_GEN"]

            con.execute(sql(f"""
                INSERT INTO dbo.{XWALK_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    source_route_id
                )
                SELECT
                    {lab_offset}
                    + ROW_NUMBER() OVER (
                        ORDER BY LTRIM(RTRIM(CONVERT(
                          nvarchar(255), l.LAB_RESULT_CM_ID
                        )))
                    ),
                    'LAB_RESULT_CM',
                    LTRIM(RTRIM(CONVERT(
                      nvarchar(255), l.LAB_RESULT_CM_ID
                    ))),
                    NULL,
                    NULL
                FROM dbo.PCORnet_LAB_RESULT_CM l
                JOIN dbo.concept c
                  ON c.vocabulary_id = 'LOINC'
                 AND c.concept_code =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.LAB_LOINC
                     )))
                 AND c.standard_concept = 'S'
                 AND c.invalid_reason IS NULL
                 AND c.domain_id = 'Observation'
            """))

            procedure_offset = lab_offset + counts["LAB_RESULT_CM"]

            con.execute(sql(f"""
                INSERT INTO dbo.{XWALK_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    source_route_id
                )
                SELECT
                    {procedure_offset}
                    + ROW_NUMBER() OVER (ORDER BY r.route_id),
                    'PROCEDURES',
                    LTRIM(RTRIM(CONVERT(
                      nvarchar(255), r.[{procedure_source_id}]
                    ))),
                    NULL,
                    r.route_id
                FROM dbo.etl_procedure_event_route r
                WHERE r.target_domain = 'Observation'
            """))

            vital_offset = procedure_offset + counts["PROCEDURES"]

            con.execute(sql(f"""
                WITH expanded AS (
                    SELECT
                        v.VITALID,
                        x.source_field,
                        x.field_order
                    FROM dbo.PCORnet_VITAL v
                    CROSS APPLY (VALUES
                        ('SMOKING', 1, v.SMOKING),
                        ('TOBACCO', 2, v.TOBACCO),
                        ('TOBACCO_TYPE', 3, v.TOBACCO_TYPE)
                    ) x(source_field, field_order, source_value)
                    WHERE x.source_value IS NOT NULL
                )
                INSERT INTO dbo.{XWALK_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    source_route_id
                )
                SELECT
                    {vital_offset}
                    + ROW_NUMBER() OVER (
                        ORDER BY
                            LTRIM(RTRIM(CONVERT(
                              nvarchar(255), VITALID
                            ))),
                            field_order
                    ),
                    'VITAL',
                    LTRIM(RTRIM(CONVERT(nvarchar(255), VITALID))),
                    source_field,
                    NULL
                FROM expanded
            """))

            lineage_rows = int(
                con.execute(
                    sql(f"SELECT COUNT_BIG(*) FROM dbo.{XWALK_TABLE}")
                ).scalar_one()
            )
            if lineage_rows != expected_total:
                raise RuntimeError(
                    "Observation xwalk reconciliation failed: "
                    f"{lineage_rows:,} != {expected_total:,}"
                )

            obsclin_dt = _safe_datetime_sql(
                "o.OBSCLIN_START_DATE",
                "o.OBSCLIN_START_TIME",
            )
            obsgen_dt = _safe_datetime_sql(
                "o.OBSGEN_START_DATE",
                "o.OBSGEN_START_TIME",
            )
            lab_dt = _safe_datetime_sql(
                "l.RESULT_DATE",
                "l.RESULT_TIME",
            )
            vital_dt = _safe_datetime_sql(
                "e.MEASURE_DATE",
                "e.MEASURE_TIME",
            )

            # OBS_CLIN -> Observation
            obsclin_value = """
                COALESCE(
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.RAW_OBSCLIN_RESULT
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSCLIN_RESULT_TEXT
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSCLIN_RESULT_SNOMED
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSCLIN_RESULT_QUAL
                    ))), '')
                )
            """

            con.execute(sql(f"""
                INSERT INTO dbo.observation (
                    observation_id,
                    person_id,
                    observation_concept_id,
                    observation_date,
                    observation_datetime,
                    observation_type_concept_id,
                    value_as_number,
                    value_as_string,
                    value_as_concept_id,
                    qualifier_concept_id,
                    unit_concept_id,
                    provider_id,
                    visit_occurrence_id,
                    visit_detail_id,
                    observation_source_value,
                    observation_source_concept_id,
                    unit_source_value,
                    qualifier_source_value,
                    value_source_value,
                    observation_event_id,
                    obs_event_field_concept_id
                )
                SELECT
                    x.observation_id,
                    p.person_id,
                    CONVERT(int, r.target_concept_id),
                    CAST(o.OBSCLIN_START_DATE AS date),
                    {obsclin_dt},
                    0,
                    TRY_CONVERT(float, o.OBSCLIN_RESULT_NUM),
                    LEFT({obsclin_value}, {string_max}),
                    0,
                    0,
                    0,
                    NULL,
                    vx.visit_occurrence_id,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), o.OBSCLIN_CODE
                    ), {source_max}),
                    CONVERT(int, r.source_concept_id),
                    LEFT(CONVERT(
                      nvarchar(max), o.OBSCLIN_RESULT_UNIT
                    ), {unit_max}),
                    NULL,
                    LEFT({obsclin_value}, {value_max}),
                    NULL,
                    0
                FROM dbo.etl_obs_clin_route r
                JOIN dbo.PCORnet_OBS_CLIN o
                  ON r.source_obsclin_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.OBSCLINID
                     )))
                JOIN dbo.{XWALK_TABLE} x
                  ON x.source_family = 'OBS_CLIN'
                 AND x.source_record_id = r.source_obsclin_id
                JOIN dbo.person p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.PATID
                     )))
                LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                  ON vx.encounterid =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.ENCOUNTERID
                     )))
                WHERE r.target_domain = 'Observation'
            """))

            # OBS_GEN. PC_COVID 2000/3000 are preserved with concept 0
            # until an exact standard concept is prespecified.
            obsgen_value = """
                COALESCE(
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSGEN_RESULT_TEXT
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), o.OBSGEN_RESULT_QUAL
                    ))), '')
                )
            """

            con.execute(sql(f"""
                INSERT INTO dbo.observation (
                    observation_id,
                    person_id,
                    observation_concept_id,
                    observation_date,
                    observation_datetime,
                    observation_type_concept_id,
                    value_as_number,
                    value_as_string,
                    value_as_concept_id,
                    qualifier_concept_id,
                    unit_concept_id,
                    provider_id,
                    visit_occurrence_id,
                    visit_detail_id,
                    observation_source_value,
                    observation_source_concept_id,
                    unit_source_value,
                    qualifier_source_value,
                    value_source_value,
                    observation_event_id,
                    obs_event_field_concept_id
                )
                SELECT
                    x.observation_id,
                    p.person_id,
                    0,
                    CAST(o.OBSGEN_START_DATE AS date),
                    {obsgen_dt},
                    0,
                    TRY_CONVERT(float, o.OBSGEN_RESULT_NUM),
                    LEFT({obsgen_value}, {string_max}),
                    0,
                    0,
                    0,
                    NULL,
                    vx.visit_occurrence_id,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), o.OBSGEN_CODE
                    ), {source_max}),
                    0,
                    LEFT(CONVERT(
                      nvarchar(max), o.OBSGEN_RESULT_UNIT
                    ), {unit_max}),
                    NULL,
                    LEFT({obsgen_value}, {value_max}),
                    NULL,
                    0
                FROM dbo.PCORnet_OBS_GEN o
                JOIN dbo.{XWALK_TABLE} x
                  ON x.source_family = 'OBS_GEN'
                 AND x.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.OBSGENID
                     )))
                JOIN dbo.person p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.PATID
                     )))
                LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                  ON vx.encounterid =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.ENCOUNTERID
                     )))
            """))

            # LAB -> Observation
            lab_value = """
                COALESCE(
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), l.RAW_RESULT
                    ))), ''),
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(max), l.RESULT_QUAL
                    ))), '')
                )
            """

            con.execute(sql(f"""
                INSERT INTO dbo.observation (
                    observation_id,
                    person_id,
                    observation_concept_id,
                    observation_date,
                    observation_datetime,
                    observation_type_concept_id,
                    value_as_number,
                    value_as_string,
                    value_as_concept_id,
                    qualifier_concept_id,
                    unit_concept_id,
                    provider_id,
                    visit_occurrence_id,
                    visit_detail_id,
                    observation_source_value,
                    observation_source_concept_id,
                    unit_source_value,
                    qualifier_source_value,
                    value_source_value,
                    observation_event_id,
                    obs_event_field_concept_id
                )
                SELECT
                    x.observation_id,
                    p.person_id,
                    c.concept_id,
                    CAST(l.RESULT_DATE AS date),
                    {lab_dt},
                    0,
                    TRY_CONVERT(float, l.RESULT_NUM),
                    LEFT({lab_value}, {string_max}),
                    0,
                    0,
                    0,
                    NULL,
                    vx.visit_occurrence_id,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), l.LAB_LOINC
                    ), {source_max}),
                    c.concept_id,
                    LEFT(CONVERT(
                      nvarchar(max), l.RESULT_UNIT
                    ), {unit_max}),
                    NULL,
                    LEFT({lab_value}, {value_max}),
                    NULL,
                    0
                FROM dbo.PCORnet_LAB_RESULT_CM l
                JOIN dbo.concept c
                  ON c.vocabulary_id = 'LOINC'
                 AND c.concept_code =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.LAB_LOINC
                     )))
                 AND c.standard_concept = 'S'
                 AND c.invalid_reason IS NULL
                 AND c.domain_id = 'Observation'
                JOIN dbo.{XWALK_TABLE} x
                  ON x.source_family = 'LAB_RESULT_CM'
                 AND x.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.LAB_RESULT_CM_ID
                     )))
                JOIN dbo.person p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.PATID
                     )))
                LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                  ON vx.encounterid =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.ENCOUNTERID
                     )))
            """))

            # PROCEDURES -> Observation
            con.execute(sql(f"""
                INSERT INTO dbo.observation (
                    observation_id,
                    person_id,
                    observation_concept_id,
                    observation_date,
                    observation_datetime,
                    observation_type_concept_id,
                    value_as_number,
                    value_as_string,
                    value_as_concept_id,
                    qualifier_concept_id,
                    unit_concept_id,
                    provider_id,
                    visit_occurrence_id,
                    visit_detail_id,
                    observation_source_value,
                    observation_source_concept_id,
                    unit_source_value,
                    qualifier_source_value,
                    value_source_value,
                    observation_event_id,
                    obs_event_field_concept_id
                )
                SELECT
                    x.observation_id,
                    p.person_id,
                    CONVERT(int, r.target_concept_id),
                    CAST(pr.PX_DATE AS date),
                    CAST(CAST(pr.PX_DATE AS date) AS datetime2(7)),
                    0,
                    NULL,
                    NULL,
                    0,
                    0,
                    0,
                    NULL,
                    vx.visit_occurrence_id,
                    NULL,
                    LEFT(CONVERT(nvarchar(max), pr.PX), {source_max}),
                    CONVERT(
                      int,
                      COALESCE({procedure_source_concept_expr}, 0)
                    ),
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    0
                FROM dbo.etl_procedure_event_route r
                JOIN dbo.PCORnet_PROCEDURES pr
                  ON LTRIM(RTRIM(CONVERT(
                       nvarchar(255), r.[{procedure_source_id}]
                     ))) =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), pr.PROCEDURESID
                     )))
                JOIN dbo.{XWALK_TABLE} x
                  ON x.source_family = 'PROCEDURES'
                 AND x.source_route_id = r.route_id
                JOIN dbo.person p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), pr.PATID
                     )))
                LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                  ON vx.encounterid =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), pr.ENCOUNTERID
                     )))
                WHERE r.target_domain = 'Observation'
            """))

            # VITAL categorical observations
            con.execute(sql(f"""
                WITH expanded AS (
                    SELECT
                        v.VITALID,
                        v.PATID,
                        v.ENCOUNTERID,
                        v.MEASURE_DATE,
                        v.MEASURE_TIME,
                        x.source_field,
                        x.source_value,
                        x.observation_concept_id,
                        x.value_as_concept_id
                    FROM dbo.PCORnet_VITAL v
                    CROSS APPLY (VALUES
                        (
                          'SMOKING',
                          v.SMOKING,
                          43054909,
                          CASE v.SMOKING
                            WHEN '01' THEN 45881517
                            WHEN '02' THEN 45884037
                            WHEN '03' THEN 45883458
                            WHEN '04' THEN 45879404
                            WHEN '05' THEN 45881518
                            WHEN '06' THEN 45885135
                            WHEN '07' THEN 45884038
                            WHEN '08' THEN 45878118
                            ELSE 0
                          END
                        ),
                        ('TOBACCO', v.TOBACCO, 3039561, 0),
                        (
                          'TOBACCO_TYPE',
                          v.TOBACCO_TYPE,
                          42528924,
                          CASE v.TOBACCO_TYPE
                            WHEN '01' THEN 42530793
                            WHEN '03' THEN 42531020
                            WHEN '05' THEN 42530756
                            ELSE 0
                          END
                        )
                    ) x(
                        source_field,
                        source_value,
                        observation_concept_id,
                        value_as_concept_id
                    )
                    WHERE x.source_value IS NOT NULL
                )
                INSERT INTO dbo.observation (
                    observation_id,
                    person_id,
                    observation_concept_id,
                    observation_date,
                    observation_datetime,
                    observation_type_concept_id,
                    value_as_number,
                    value_as_string,
                    value_as_concept_id,
                    qualifier_concept_id,
                    unit_concept_id,
                    provider_id,
                    visit_occurrence_id,
                    visit_detail_id,
                    observation_source_value,
                    observation_source_concept_id,
                    unit_source_value,
                    qualifier_source_value,
                    value_source_value,
                    observation_event_id,
                    obs_event_field_concept_id
                )
                SELECT
                    xw.observation_id,
                    p.person_id,
                    e.observation_concept_id,
                    CAST(e.MEASURE_DATE AS date),
                    {vital_dt},
                    0,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), e.source_value
                    ), {string_max}),
                    e.value_as_concept_id,
                    0,
                    0,
                    NULL,
                    vx.visit_occurrence_id,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), e.source_field
                    ), {source_max}),
                    0,
                    NULL,
                    NULL,
                    LEFT(CONVERT(
                      nvarchar(max), e.source_value
                    ), {value_max}),
                    NULL,
                    0
                FROM expanded e
                JOIN dbo.{XWALK_TABLE} xw
                  ON xw.source_family = 'VITAL'
                 AND xw.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), e.VITALID
                     )))
                 AND xw.source_field = e.source_field
                JOIN dbo.person p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), e.PATID
                     )))
                LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                  ON vx.encounterid =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), e.ENCOUNTERID
                     )))
            """))

            target_rows = int(
                con.execute(
                    sql("SELECT COUNT_BIG(*) FROM dbo.observation")
                ).scalar_one()
            )

            if target_rows != expected_total:
                raise RuntimeError(
                    "Observation target reconciliation failed: "
                    f"{target_rows:,} != {expected_total:,}"
                )

            # Preserve source strings that exceed value_source_value.
            con.execute(sql(f"""
                INSERT INTO dbo.{OVERFLOW_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    full_source_value,
                    source_length
                )
                SELECT
                    x.observation_id,
                    'OBS_CLIN',
                    x.source_record_id,
                    NULL,
                    {obsclin_value},
                    LEN({obsclin_value})
                FROM dbo.{XWALK_TABLE} x
                JOIN dbo.PCORnet_OBS_CLIN o
                  ON x.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.OBSCLINID
                     )))
                WHERE x.source_family = 'OBS_CLIN'
                  AND LEN({obsclin_value}) > {value_max}
            """))

            con.execute(sql(f"""
                INSERT INTO dbo.{OVERFLOW_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    full_source_value,
                    source_length
                )
                SELECT
                    x.observation_id,
                    'OBS_GEN',
                    x.source_record_id,
                    NULL,
                    {obsgen_value},
                    LEN({obsgen_value})
                FROM dbo.{XWALK_TABLE} x
                JOIN dbo.PCORnet_OBS_GEN o
                  ON x.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.OBSGENID
                     )))
                WHERE x.source_family = 'OBS_GEN'
                  AND LEN({obsgen_value}) > {value_max}
            """))

            con.execute(sql(f"""
                INSERT INTO dbo.{OVERFLOW_TABLE} (
                    observation_id,
                    source_family,
                    source_record_id,
                    source_field,
                    full_source_value,
                    source_length
                )
                SELECT
                    x.observation_id,
                    'LAB_RESULT_CM',
                    x.source_record_id,
                    NULL,
                    {lab_value},
                    LEN({lab_value})
                FROM dbo.{XWALK_TABLE} x
                JOIN dbo.PCORnet_LAB_RESULT_CM l
                  ON x.source_record_id =
                     LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.LAB_RESULT_CM_ID
                     )))
                WHERE x.source_family = 'LAB_RESULT_CM'
                  AND LEN({lab_value}) > {value_max}
            """))

            concept_zero_rows = int(
                con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.observation
                        WHERE observation_concept_id = 0
                    """)
                ).scalar_one()
            )

            if concept_zero_rows != expected_concept_zero:
                raise RuntimeError(
                    "Unexpected observation_concept_id=0 count: "
                    f"{concept_zero_rows:,}; source-derived expected "
                    f"{expected_concept_zero:,}"
                )

            vital_value_zero = int(
                con.execute(
                    sql(f"""
                        SELECT COUNT_BIG(*)
                        FROM dbo.observation o
                        JOIN dbo.{XWALK_TABLE} x
                          ON x.observation_id = o.observation_id
                        WHERE x.source_family = 'VITAL'
                          AND o.value_as_concept_id = 0
                    """)
                ).scalar_one()
            )

            if vital_value_zero != expected_vital_value_zero:
                raise RuntimeError(
                    "Unexpected VITAL value concept-0 count: "
                    f"{vital_value_zero:,}; source-derived expected "
                    f"{expected_vital_value_zero:,}"
                )

            visit_linked_rows = int(
                con.execute(
                    sql("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.observation
                        WHERE visit_occurrence_id IS NOT NULL
                    """)
                ).scalar_one()
            )

            overflow_rows = int(
                con.execute(
                    sql(f"SELECT COUNT_BIG(*) FROM dbo.{OVERFLOW_TABLE}")
                ).scalar_one()
            )

            family_target = {
                str(r[0]): int(r[1])
                for r in con.execute(
                    sql(f"""
                        SELECT
                            x.source_family,
                            COUNT_BIG(*)
                        FROM dbo.observation o
                        JOIN dbo.{XWALK_TABLE} x
                          ON x.observation_id = o.observation_id
                        GROUP BY x.source_family
                    """)
                ).fetchall()
            }

            for family, expected in counts.items():
                if family_target.get(family, 0) != expected:
                    raise RuntimeError(
                        f"{family} target reconciliation failed: "
                        f"{family_target.get(family, 0):,} "
                        f"!= source-derived {expected:,}"
                    )

            status = "matched"

        payload = {
            "stage": "observation",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "source_family_expected_rows": counts,
            "source_family_target_rows": family_target,
            "expected_rows": expected_total,
            "target_rows": target_rows,
            "lineage_rows": lineage_rows,
            "observation_concept_zero_rows": concept_zero_rows,
            "expected_observation_concept_zero_rows": expected_concept_zero,
            "vital_value_concept_zero_rows": vital_value_zero,
            "expected_vital_value_concept_zero_rows": expected_vital_value_zero,
            "visit_linked_rows": visit_linked_rows,
            "overflow_rows": overflow_rows,
            "policies": {
                "obs_clin": (
                    "Use OBS_CLIN domain route ledger; no cross-source "
                    "deduplication"
                ),
                "obs_gen": (
                    "Retain PC_COVID 2000 and 3000, including N; "
                    "observation_concept_id=0 pending an exact "
                    "prespecified standard concept"
                ),
                "lab": (
                    "Direct active standard LOINC concepts whose OMOP "
                    "domain is Observation"
                ),
                "procedures": (
                    "Use procedure event route ledger, including unresolved "
                    "Observation-domain routes"
                ),
                "vital": (
                    "SMOKING, TOBACCO and TOBACCO_TYPE only; BP_POSITION NI "
                    "does not generate an event"
                ),
                "text": (
                    "value_source_value projected to the installed OMOP "
                    "column width; longer source values retained in local "
                    "overflow lineage"
                ),
                "schemas": (
                    "PCORnet staging tables use configured source_schema; "
                    "OMOP and ETL lineage/route tables use configured "
                    "target_schema"
                ),
            },
            "status": status,
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return ObservationTransformResult(
            obs_clin_rows=family_target["OBS_CLIN"],
            obs_gen_rows=family_target["OBS_GEN"],
            lab_rows=family_target["LAB_RESULT_CM"],
            procedure_rows=family_target["PROCEDURES"],
            vital_rows=family_target["VITAL"],
            expected_rows=expected_total,
            target_rows=target_rows,
            lineage_rows=lineage_rows,
            concept_zero_rows=concept_zero_rows,
            vital_value_concept_zero_rows=vital_value_zero,
            visit_linked_rows=visit_linked_rows,
            overflow_rows=overflow_rows,
            status=status,
            audit_path=str(audit_path),
        )

    finally:
        engine.dispose()
