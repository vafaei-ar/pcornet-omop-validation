from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


# PCORnet ENC_TYPE -> broad, standard OMOP Visit concepts.
#
# These mappings use only concepts from the standard OMOP Visit hierarchy and
# avoid network-specific/PEDSnet extension concepts. Where PCORnet semantics do
# not establish a defensible OMOP Visit concept, the primary ETL uses 0 and
# preserves ENC_TYPE in visit_source_value.
VISIT_CONCEPT_MAP = {
    "AV": 9202,       # Ambulatory Visit -> Outpatient Visit
    "ED": 9203,       # Emergency Department -> Emergency Room Visit
    "EI": 262,        # Combined ED + inpatient -> ER and Inpatient Visit
    "IP": 9201,       # Inpatient Hospital Stay -> Inpatient Visit
    "IS": 42898160,   # Non-Acute Institutional Stay -> Non-hospital institution Visit
    "OS": 581385,     # Observation Stay -> Observation Room
    "IC": 0,          # Institutional Professional Consult does not establish visit setting
    "TH": 5083,       # Telehealth -> Telehealth Visit
    "OA": 9202,       # Other Ambulatory Visit -> broad Outpatient Visit
}


@dataclass(frozen=True)
class VisitOccurrenceTransformResult:
    source_rows: int
    eligible_rows: int
    excluded_rows: int
    excluded_missing_encounterid: int
    excluded_missing_patid: int
    excluded_unlinked_person: int
    excluded_missing_admit_date: int
    excluded_missing_discharge_date: int
    excluded_invalid_interval: int
    target_rows: int
    visit_concept_zero: int
    visit_source_concept_zero: int
    unknown_enc_type_rows: int
    crosswalk_rows: int
    status: str
    audit_path: Path


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_ENCOUNTER"),
        (target_schema, "person"),
        (target_schema, "visit_occurrence"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _validate_visit_concepts(connection, schema: str) -> None:
    ids = sorted({value for value in VISIT_CONCEPT_MAP.values() if value != 0})
    values = ",".join(str(value) for value in ids)
    invalid = connection.execute(
        text(
            f"""
            SELECT concept_id, concept_name, domain_id, standard_concept, invalid_reason
            FROM [{schema}].[concept]
            WHERE concept_id IN ({values})
              AND NOT (
                    domain_id = 'Visit'
                AND standard_concept = 'S'
                AND invalid_reason IS NULL
              )
            """
        )
    ).fetchall()
    present = {
        int(row[0])
        for row in connection.execute(
            text(f"SELECT concept_id FROM [{schema}].[concept] WHERE concept_id IN ({values})")
        ).fetchall()
    }
    missing = sorted(set(ids) - present)
    if missing or invalid:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(str(x) for x in missing))
        if invalid:
            details.append(
                "not_active_standard_visit="
                + "; ".join(
                    f"{row[0]}:{row[2]}:{row[3]}:{row[4]}" for row in invalid
                )
            )
        raise RuntimeError(
            "Configured visit concept mapping failed vocabulary validation: "
            + " | ".join(details)
        )


def _case_sql(column: str, mapping: dict[str, int], default: int = 0) -> str:
    clauses = " ".join(
        f"WHEN {column} = '{key}' THEN {value}" for key, value in mapping.items()
    )
    return f"CASE {clauses} ELSE {default} END"


def _datetime_sql(date_column: str, time_column: str) -> str:
    return f"""
    CASE
      WHEN {date_column} IS NULL THEN NULL
      WHEN TRY_CAST({time_column} AS time(7)) IS NULL THEN CAST({date_column} AS datetime2(7))
      ELSE TRY_CONVERT(
        datetime2(7),
        CONVERT(char(10), CAST({date_column} AS date), 23) + ' ' +
        CONVERT(varchar(30), TRY_CAST({time_column} AS time(7)))
      )
    END
    """.strip()


def transform_visit_occurrence(config: EtlConfig) -> VisitOccurrenceTransformResult:
    """Transform staged PCORnet ENCOUNTER records into OMOP visit_occurrence.

    The validated primary ETL excludes encounters missing required start/end dates rather
    than manufacturing sentinel dates. It preserves encounter lineage in a separate
    ETL crosswalk table and leaves provider/care_site linkage null until those dimensions
    are built. Unsupported or semantically ambiguous ENC_TYPE values remain in
    visit_source_value and map to visit_concept_id 0.
    """
    policies = config.raw.get("policies", {}) or {}
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "The validated visit stage currently implements policies.missing_required_date=exclude only"
        )
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "The validated visit stage currently implements policies.unmapped_standard_concept=concept_zero only"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "visit_occurrence_transform.json"
    xwalk_table = "etl_visit_occurrence_xwalk"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)
            _validate_visit_concepts(connection, target_schema)

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_ENCOUNTER]",
            )

            duplicate_encounterids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*) FROM (
                    SELECT ENCOUNTERID
                    FROM [{source_schema}].[PCORnet_ENCOUNTER]
                    WHERE ENCOUNTERID IS NOT NULL
                      AND LTRIM(RTRIM(CAST(ENCOUNTERID AS nvarchar(max)))) <> ''
                    GROUP BY ENCOUNTERID
                    HAVING COUNT_BIG(*) > 1
                ) d
                """,
            )
            if duplicate_encounterids:
                raise RuntimeError(
                    f"PCORnet_ENCOUNTER contains {duplicate_encounterids:,} duplicated ENCOUNTERID value(s); "
                    "visit lineage would be ambiguous"
                )

            classification_cte = f"""
            WITH classified AS (
              SELECT e.*,
                     p.person_id,
                     CASE
                       WHEN e.ENCOUNTERID IS NULL OR LTRIM(RTRIM(CAST(e.ENCOUNTERID AS nvarchar(max)))) = ''
                         THEN 'missing_encounterid'
                       WHEN e.PATID IS NULL OR LTRIM(RTRIM(CAST(e.PATID AS nvarchar(max)))) = ''
                         THEN 'missing_patid'
                       WHEN p.person_id IS NULL THEN 'unlinked_person'
                       WHEN e.ADMIT_DATE IS NULL THEN 'missing_admit_date'
                       WHEN e.DISCHARGE_DATE IS NULL THEN 'missing_discharge_date'
                       WHEN CAST(e.DISCHARGE_DATE AS date) < CAST(e.ADMIT_DATE AS date)
                         THEN 'invalid_interval'
                       ELSE 'eligible'
                     END AS record_status
              FROM [{source_schema}].[PCORnet_ENCOUNTER] e
              LEFT JOIN [{target_schema}].[person] p
                ON CAST(e.PATID AS nvarchar(50)) = p.person_source_value
            )
            """

            counts = dict(
                connection.execute(
                    text(
                        classification_cte
                        + " SELECT record_status, COUNT_BIG(*) AS n FROM classified GROUP BY record_status"
                    )
                ).fetchall()
            )
            eligible_rows = int(counts.get("eligible", 0))
            excluded_missing_encounterid = int(counts.get("missing_encounterid", 0))
            excluded_missing_patid = int(counts.get("missing_patid", 0))
            excluded_unlinked_person = int(counts.get("unlinked_person", 0))
            excluded_missing_admit_date = int(counts.get("missing_admit_date", 0))
            excluded_missing_discharge_date = int(counts.get("missing_discharge_date", 0))
            excluded_invalid_interval = int(counts.get("invalid_interval", 0))
            excluded_rows = source_rows - eligible_rows

            known_types_sql = ",".join(repr(x) for x in VISIT_CONCEPT_MAP)
            unknown_enc_type_rows = _scalar(
                connection,
                f"""
                {classification_cte}
                SELECT COUNT_BIG(*)
                FROM classified
                WHERE record_status = 'eligible'
                  AND (ENC_TYPE IS NULL OR ENC_TYPE NOT IN ({known_types_sql}))
                """,
            )

            existing = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]",
            )
            if existing:
                if existing != eligible_rows:
                    raise RuntimeError(
                        f"Target [{target_schema}].[visit_occurrence] already contains {existing:,} rows but "
                        f"validated eligible source count is {eligible_rows:,}; refusing to overwrite"
                    )
                status = "already_loaded_matched"
            else:
                visit_concept_case = _case_sql("ENC_TYPE", VISIT_CONCEPT_MAP)
                start_datetime = _datetime_sql("ADMIT_DATE", "ADMIT_TIME")
                end_datetime = _datetime_sql("DISCHARGE_DATE", "DISCHARGE_TIME")

                insert_sql = f"""
                {classification_cte},
                eligible AS (
                  SELECT
                    ROW_NUMBER() OVER (ORDER BY CAST(ENCOUNTERID AS nvarchar(255))) AS visit_occurrence_id,
                    *
                  FROM classified
                  WHERE record_status = 'eligible'
                )
                INSERT INTO [{target_schema}].[visit_occurrence] (
                    visit_occurrence_id,
                    person_id,
                    visit_concept_id,
                    visit_start_date,
                    visit_start_datetime,
                    visit_end_date,
                    visit_end_datetime,
                    visit_type_concept_id,
                    provider_id,
                    care_site_id,
                    visit_source_value,
                    visit_source_concept_id,
                    admitted_from_concept_id,
                    admitted_from_source_value,
                    discharged_to_concept_id,
                    discharged_to_source_value
                )
                SELECT
                    visit_occurrence_id,
                    person_id,
                    {visit_concept_case},
                    CAST(ADMIT_DATE AS date),
                    {start_datetime},
                    CAST(DISCHARGE_DATE AS date),
                    {end_datetime},
                    0,
                    NULL,
                    NULL,
                    CAST(ENC_TYPE AS nvarchar(50)),
                    0,
                    0,
                    CAST(ADMITTING_SOURCE AS nvarchar(50)),
                    0,
                    CAST(DISCHARGE_DISPOSITION AS nvarchar(50))
                FROM eligible
                """
                connection.exec_driver_sql(insert_sql)
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]",
            )
            if target_rows != eligible_rows:
                raise RuntimeError(
                    f"Visit reconciliation failed: eligible_source={eligible_rows:,}, target={target_rows:,}"
                )

            if table_exists(connection, source_schema, xwalk_table):
                crosswalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{xwalk_table}]",
                )
                if crosswalk_rows != eligible_rows:
                    raise RuntimeError(
                        f"Existing visit crosswalk has {crosswalk_rows:,} rows but expected {eligible_rows:,}"
                    )
            else:
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE [{source_schema}].[{xwalk_table}] (
                        encounterid nvarchar(255) NOT NULL,
                        visit_occurrence_id bigint NOT NULL,
                        CONSTRAINT PK_{xwalk_table} PRIMARY KEY (encounterid),
                        CONSTRAINT UQ_{xwalk_table}_visit UNIQUE (visit_occurrence_id)
                    )
                    """
                )
                connection.exec_driver_sql(
                    f"""
                    {classification_cte},
                    eligible AS (
                      SELECT
                        ROW_NUMBER() OVER (ORDER BY CAST(ENCOUNTERID AS nvarchar(255))) AS visit_occurrence_id,
                        ENCOUNTERID
                      FROM classified
                      WHERE record_status = 'eligible'
                    )
                    INSERT INTO [{source_schema}].[{xwalk_table}] (encounterid, visit_occurrence_id)
                    SELECT CAST(ENCOUNTERID AS nvarchar(255)), visit_occurrence_id
                    FROM eligible
                    """
                )
                connection.commit()
                crosswalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{xwalk_table}]",
                )

            if crosswalk_rows != target_rows:
                raise RuntimeError(
                    f"Visit crosswalk reconciliation failed: crosswalk={crosswalk_rows:,}, target={target_rows:,}"
                )

            visit_concept_zero = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence] WHERE visit_concept_id = 0",
            )
            visit_source_concept_zero = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence] WHERE visit_source_concept_id = 0",
            )
    finally:
        engine.dispose()

    result = VisitOccurrenceTransformResult(
        source_rows=source_rows,
        eligible_rows=eligible_rows,
        excluded_rows=excluded_rows,
        excluded_missing_encounterid=excluded_missing_encounterid,
        excluded_missing_patid=excluded_missing_patid,
        excluded_unlinked_person=excluded_unlinked_person,
        excluded_missing_admit_date=excluded_missing_admit_date,
        excluded_missing_discharge_date=excluded_missing_discharge_date,
        excluded_invalid_interval=excluded_invalid_interval,
        target_rows=target_rows,
        visit_concept_zero=visit_concept_zero,
        visit_source_concept_zero=visit_source_concept_zero,
        unknown_enc_type_rows=unknown_enc_type_rows,
        crosswalk_rows=crosswalk_rows,
        status=status,
        audit_path=audit_path,
    )

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "visit_occurrence",
                "source_table": f"{source_schema}.PCORnet_ENCOUNTER",
                "target_table": f"{target_schema}.visit_occurrence",
                "lineage_table": f"{source_schema}.{xwalk_table}",
                "policies": {
                    "missing_required_date": policies.get("missing_required_date"),
                    "unmapped_standard_concept": policies.get("unmapped_standard_concept"),
                },
                "mapping_strategy": {
                    "id": "deterministic ROW_NUMBER ordered by ENCOUNTERID in clean target",
                    "visit_concept": (
                        "PCORnet ENC_TYPE mapped to broad active Standard OMOP Visit concepts; "
                        "ambiguous IC and unsupported values use concept_id 0"
                    ),
                    "visit_source_concept": (
                        "0 because PCORnet ENC_TYPE is a local CDM categorical code without a "
                        "corresponding source vocabulary concept in the loaded OMOP vocabulary; "
                        "the exact code is preserved in visit_source_value"
                    ),
                    "visit_type_concept": "0 because PCORnet ENCOUNTER alone does not establish OMOP provenance type",
                    "provider_id": "NULL pending provider source availability",
                    "care_site_id": "NULL pending audited care_site/location stage",
                    "missing_end_date": "exclude; no sentinel date in primary validated ETL",
                    "admitting_discharge_concepts": "0 pending separate semantic validation; source values preserved",
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
