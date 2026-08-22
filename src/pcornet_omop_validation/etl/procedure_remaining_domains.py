from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


EXPECTED_ROUTE_ROWS = {
    "Condition": 1_210,
    "Device": 196_230,
    "Specimen": 47,
}

BASELINE_CONDITION_ROWS = 8_714_088
FINAL_CONDITION_ROWS = BASELINE_CONDITION_ROWS + EXPECTED_ROUTE_ROWS["Condition"]
GENERIC_EHR_TYPE_CONCEPT_ID = 32817
DEVICE_XWALK = "etl_device_exposure_xwalk"
SPECIMEN_XWALK = "etl_specimen_xwalk"
CONDITION_XWALK = "etl_condition_occurrence_xwalk"
ROUTE_TABLE = "etl_procedure_event_route"


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, ROUTE_TABLE),
        (source_schema, CONDITION_XWALK),
        (target_schema, "person"),
        (target_schema, "condition_occurrence"),
        (target_schema, "device_exposure"),
        (target_schema, "specimen"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _route_count(connection, schema: str, domain: str) -> int:
    return _scalar(
        connection,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{schema}].[{ROUTE_TABLE}]
        WHERE target_domain = :domain
        """,
        {"domain": domain},
    )


def _distinct_route_count(connection, schema: str, domain: str) -> int:
    return _scalar(
        connection,
        f"""
        SELECT COUNT_BIG(DISTINCT source_procedure_id)
        FROM [{schema}].[{ROUTE_TABLE}]
        WHERE target_domain = :domain
        """,
        {"domain": domain},
    )


def materialize_procedure_remaining_domains(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_remaining_domains.json"

    engine = make_engine(config)
    try:
        with engine.begin() as connection:
            _require_tables(connection, source_schema, target_schema)

            type_row = connection.execute(
                text(
                    f"""
                    SELECT domain_id, standard_concept, invalid_reason
                    FROM [{target_schema}].[concept]
                    WHERE concept_id = :concept_id
                    """
                ),
                {"concept_id": GENERIC_EHR_TYPE_CONCEPT_ID},
            ).one_or_none()
            if type_row is None:
                raise RuntimeError(
                    f"Type concept {GENERIC_EHR_TYPE_CONCEPT_ID} is missing"
                )
            if type_row[0] != "Type Concept" or type_row[2] is not None:
                raise RuntimeError(
                    f"Type concept {GENERIC_EHR_TYPE_CONCEPT_ID} is not valid"
                )

            for domain, expected in EXPECTED_ROUTE_ROWS.items():
                route_rows = _route_count(connection, source_schema, domain)
                distinct_rows = _distinct_route_count(connection, source_schema, domain)
                if route_rows != expected:
                    raise RuntimeError(
                        f"{domain} route count changed: {route_rows:,} != {expected:,}"
                    )
                if distinct_rows != route_rows:
                    raise RuntimeError(
                        f"{domain} is no longer one route per source event: "
                        f"routes={route_rows:,}, distinct_sources={distinct_rows:,}"
                    )

            person_unlinked = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[person] p
                  ON p.person_source_value = r.patid
                WHERE r.target_domain IN ('Condition','Device','Specimen')
                  AND p.person_id IS NULL
                """,
            )
            if person_unlinked:
                raise RuntimeError(
                    f"Remaining procedure routes have {person_unlinked:,} unlinked persons"
                )

            condition_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
            )
            condition_xwalk_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{CONDITION_XWALK}]",
            )
            condition_procedure_xwalk = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[{CONDITION_XWALK}]
                WHERE source_domain = 'PROCEDURES'
                """,
            )
            device_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure]",
            )
            specimen_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen]",
            )
            device_xwalk_exists = table_exists(connection, source_schema, DEVICE_XWALK)
            specimen_xwalk_exists = table_exists(connection, source_schema, SPECIMEN_XWALK)
            device_xwalk_rows = (
                _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{DEVICE_XWALK}]",
                )
                if device_xwalk_exists
                else 0
            )
            specimen_xwalk_rows = (
                _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{SPECIMEN_XWALK}]",
                )
                if specimen_xwalk_exists
                else 0
            )

            already_matched = (
                condition_rows == FINAL_CONDITION_ROWS
                and condition_xwalk_rows == FINAL_CONDITION_ROWS
                and condition_procedure_xwalk == EXPECTED_ROUTE_ROWS["Condition"]
                and device_rows == EXPECTED_ROUTE_ROWS["Device"]
                and device_xwalk_rows == EXPECTED_ROUTE_ROWS["Device"]
                and specimen_rows == EXPECTED_ROUTE_ROWS["Specimen"]
                and specimen_xwalk_rows == EXPECTED_ROUTE_ROWS["Specimen"]
            )
            if already_matched:
                return {
                    "stage": "procedure_remaining_domains",
                    "status": "already_matched",
                    "condition_rows": condition_rows,
                    "device_rows": device_rows,
                    "specimen_rows": specimen_rows,
                    "audit_path": str(audit_path),
                }

            pristine = (
                condition_rows == BASELINE_CONDITION_ROWS
                and condition_xwalk_rows == BASELINE_CONDITION_ROWS
                and condition_procedure_xwalk == 0
                and device_rows == 0
                and specimen_rows == 0
                and not device_xwalk_exists
                and not specimen_xwalk_exists
            )
            if not pristine:
                raise RuntimeError(
                    "Remaining-domain targets are not in the expected pristine state: "
                    f"condition={condition_rows:,}, condition_xwalk={condition_xwalk_rows:,}, "
                    f"condition_procedure_xwalk={condition_procedure_xwalk:,}, "
                    f"device={device_rows:,}, device_xwalk={device_xwalk_rows:,}, "
                    f"specimen={specimen_rows:,}, specimen_xwalk={specimen_xwalk_rows:,}"
                )

            condition_max_id = _scalar(
                connection,
                f"""
                SELECT COALESCE(MAX(condition_occurrence_id), 0)
                FROM [{target_schema}].[condition_occurrence]
                """,
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS rn,
                            r.*,
                            p.person_id,
                            v.visit_occurrence_id
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value = r.patid
                        LEFT JOIN [{source_schema}].[etl_visit_occurrence_xwalk] v
                          ON v.encounterid = r.encounterid
                        WHERE r.target_domain = 'Condition'
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
                        stop_reason,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        condition_source_value,
                        condition_source_concept_id,
                        condition_status_source_value
                    )
                    SELECT
                        :base_id + CONVERT(int, rn),
                        person_id,
                        CONVERT(int, target_concept_id),
                        px_date,
                        CAST(px_date AS datetime),
                        NULL,
                        NULL,
                        :type_id,
                        0,
                        NULL,
                        NULL,
                        visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), px), 50),
                        CONVERT(int, source_concept_id),
                        NULL
                    FROM src;
                    """
                ),
                {"base_id": condition_max_id, "type_id": GENERIC_EHR_TYPE_CONCEPT_ID},
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS rn,
                            r.*
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        WHERE r.target_domain = 'Condition'
                    )
                    INSERT INTO [{source_schema}].[{CONDITION_XWALK}] (
                        source_domain,
                        source_record_id,
                        condition_occurrence_id,
                        source_code_type,
                        source_provenance,
                        date_basis
                    )
                    SELECT
                        'PROCEDURES',
                        source_procedure_id,
                        :base_id + CONVERT(int, rn),
                        LEFT(CONVERT(nvarchar(50), px_type), 50),
                        LEFT(CONVERT(nvarchar(50), px_source), 50),
                        'PX_DATE'
                    FROM src;
                    """
                ),
                {"base_id": condition_max_id},
            )

            connection.execute(
                text(
                    f"""
                    CREATE TABLE [{source_schema}].[{DEVICE_XWALK}] (
                        source_record_id nvarchar(255) NOT NULL PRIMARY KEY,
                        device_exposure_id int NOT NULL UNIQUE,
                        source_code_type nvarchar(50) NULL,
                        source_provenance nvarchar(50) NULL,
                        date_basis varchar(32) NOT NULL
                    );
                    """
                )
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS device_exposure_id,
                            r.*,
                            p.person_id,
                            v.visit_occurrence_id
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value = r.patid
                        LEFT JOIN [{source_schema}].[etl_visit_occurrence_xwalk] v
                          ON v.encounterid = r.encounterid
                        WHERE r.target_domain = 'Device'
                    )
                    INSERT INTO [{target_schema}].[device_exposure] (
                        device_exposure_id,
                        person_id,
                        device_concept_id,
                        device_exposure_start_date,
                        device_exposure_start_datetime,
                        device_exposure_end_date,
                        device_exposure_end_datetime,
                        device_type_concept_id,
                        unique_device_id,
                        production_id,
                        quantity,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        device_source_value,
                        device_source_concept_id,
                        unit_concept_id,
                        unit_source_value,
                        unit_source_concept_id
                    )
                    SELECT
                        CONVERT(int, device_exposure_id),
                        person_id,
                        CONVERT(int, target_concept_id),
                        px_date,
                        CAST(px_date AS datetime),
                        NULL,
                        NULL,
                        :type_id,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), px), 50),
                        CONVERT(int, source_concept_id),
                        NULL,
                        NULL,
                        NULL
                    FROM src;
                    """
                ),
                {"type_id": GENERIC_EHR_TYPE_CONCEPT_ID},
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS device_exposure_id,
                            r.*
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        WHERE r.target_domain = 'Device'
                    )
                    INSERT INTO [{source_schema}].[{DEVICE_XWALK}] (
                        source_record_id,
                        device_exposure_id,
                        source_code_type,
                        source_provenance,
                        date_basis
                    )
                    SELECT
                        source_procedure_id,
                        CONVERT(int, device_exposure_id),
                        LEFT(CONVERT(nvarchar(50), px_type), 50),
                        LEFT(CONVERT(nvarchar(50), px_source), 50),
                        'PX_DATE'
                    FROM src;
                    """
                )
            )

            connection.execute(
                text(
                    f"""
                    CREATE TABLE [{source_schema}].[{SPECIMEN_XWALK}] (
                        source_record_id nvarchar(255) NOT NULL PRIMARY KEY,
                        specimen_id int NOT NULL UNIQUE,
                        source_code_type nvarchar(50) NULL,
                        source_provenance nvarchar(50) NULL,
                        date_basis varchar(32) NOT NULL
                    );
                    """
                )
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS specimen_id,
                            r.*,
                            p.person_id
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value = r.patid
                        WHERE r.target_domain = 'Specimen'
                    )
                    INSERT INTO [{target_schema}].[specimen] (
                        specimen_id,
                        person_id,
                        specimen_concept_id,
                        specimen_type_concept_id,
                        specimen_date,
                        specimen_datetime,
                        quantity,
                        unit_concept_id,
                        anatomic_site_concept_id,
                        disease_status_concept_id,
                        specimen_source_id,
                        specimen_source_value,
                        unit_source_value,
                        anatomic_site_source_value,
                        disease_status_source_value
                    )
                    SELECT
                        CONVERT(int, specimen_id),
                        person_id,
                        CONVERT(int, target_concept_id),
                        :type_id,
                        px_date,
                        CAST(px_date AS datetime),
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        LEFT(CONVERT(varchar(50), source_procedure_id), 50),
                        LEFT(CONVERT(varchar(50), px), 50),
                        NULL,
                        NULL,
                        NULL
                    FROM src;
                    """
                ),
                {"type_id": GENERIC_EHR_TYPE_CONCEPT_ID},
            )

            connection.execute(
                text(
                    f"""
                    WITH src AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                ORDER BY r.source_procedure_id, r.target_concept_id
                            ) AS specimen_id,
                            r.*
                        FROM [{source_schema}].[{ROUTE_TABLE}] r
                        WHERE r.target_domain = 'Specimen'
                    )
                    INSERT INTO [{source_schema}].[{SPECIMEN_XWALK}] (
                        source_record_id,
                        specimen_id,
                        source_code_type,
                        source_provenance,
                        date_basis
                    )
                    SELECT
                        source_procedure_id,
                        CONVERT(int, specimen_id),
                        LEFT(CONVERT(nvarchar(50), px_type), 50),
                        LEFT(CONVERT(nvarchar(50), px_source), 50),
                        'PX_DATE'
                    FROM src;
                    """
                )
            )

            final_condition_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
            )
            final_condition_xwalk = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{CONDITION_XWALK}]",
            )
            final_condition_procedure_xwalk = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[{CONDITION_XWALK}]
                WHERE source_domain = 'PROCEDURES'
                """,
            )
            final_device_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure]",
            )
            final_device_xwalk = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{DEVICE_XWALK}]",
            )
            final_specimen_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen]",
            )
            final_specimen_xwalk = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{SPECIMEN_XWALK}]",
            )

            if final_condition_rows != FINAL_CONDITION_ROWS:
                raise RuntimeError(
                    f"Condition final count mismatch: {final_condition_rows:,} != {FINAL_CONDITION_ROWS:,}"
                )
            if final_condition_xwalk != FINAL_CONDITION_ROWS:
                raise RuntimeError(
                    f"Condition lineage mismatch: {final_condition_xwalk:,} != {FINAL_CONDITION_ROWS:,}"
                )
            if final_condition_procedure_xwalk != EXPECTED_ROUTE_ROWS["Condition"]:
                raise RuntimeError("Procedure Condition lineage count mismatch")
            if final_device_rows != EXPECTED_ROUTE_ROWS["Device"] or final_device_xwalk != final_device_rows:
                raise RuntimeError("Device reconciliation failed")
            if final_specimen_rows != EXPECTED_ROUTE_ROWS["Specimen"] or final_specimen_xwalk != final_specimen_rows:
                raise RuntimeError("Specimen reconciliation failed")

            concept_zero = {
                "Condition": _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] c
                    JOIN [{source_schema}].[{CONDITION_XWALK}] x
                      ON x.condition_occurrence_id = c.condition_occurrence_id
                    WHERE x.source_domain = 'PROCEDURES'
                      AND c.condition_concept_id = 0
                    """,
                ),
                "Device": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure] WHERE device_concept_id = 0",
                ),
                "Specimen": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen] WHERE specimen_concept_id = 0",
                ),
            }
            visit_linked = {
                "Condition": _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] c
                    JOIN [{source_schema}].[{CONDITION_XWALK}] x
                      ON x.condition_occurrence_id = c.condition_occurrence_id
                    WHERE x.source_domain = 'PROCEDURES'
                      AND c.visit_occurrence_id IS NOT NULL
                    """,
                ),
                "Device": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure] WHERE visit_occurrence_id IS NOT NULL",
                ),
            }

        payload = {
            "stage": "procedure_remaining_domains",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "type_concept_id": GENERIC_EHR_TYPE_CONCEPT_ID,
            "type_concept_policy": (
                "Use generic standard EHR Type Concept 32817 for procedure-derived "
                "Condition, Device, and Specimen rows; avoid inferring claim-specific "
                "or diagnostic intent from PCORnet PROCEDURES alone."
            ),
            "condition_rows": final_condition_rows,
            "condition_lineage_rows": final_condition_xwalk,
            "condition_procedure_rows": final_condition_procedure_xwalk,
            "device_rows": final_device_rows,
            "device_lineage_rows": final_device_xwalk,
            "specimen_rows": final_specimen_rows,
            "specimen_lineage_rows": final_specimen_xwalk,
            "concept_zero_rows": concept_zero,
            "visit_linked_rows": visit_linked,
            "status": "matched",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
