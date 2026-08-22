from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


DOMAINS = ("Condition", "Device", "Specimen")
ROUTE_TABLE = "etl_procedure_event_route"
CONDITION_XWALK = "etl_condition_occurrence_xwalk"
DEVICE_XWALK = "etl_device_exposure_xwalk"
SPECIMEN_XWALK = "etl_specimen_xwalk"


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_PROCEDURES"),
        (target_schema, ROUTE_TABLE),
        (target_schema, CONDITION_XWALK),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "person"),
        (target_schema, "condition_occurrence"),
        (target_schema, "device_exposure"),
        (target_schema, "specimen"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(
                f"Required table [{schema}].[{table}] does not exist"
            )


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


def _validate_routes(connection, target_schema: str) -> dict[str, int]:
    route_counts: dict[str, int] = {}
    for domain in DOMAINS:
        rows = _route_count(connection, target_schema, domain)
        distinct_rows = _distinct_route_count(connection, target_schema, domain)
        if rows != distinct_rows:
            raise RuntimeError(
                f"{domain} currently has one-to-many routes within the same target "
                f"domain (routes={rows:,}, distinct_sources={distinct_rows:,}). "
                "The remaining-domain lineage tables are one row per source event; "
                "route-aware lineage must be implemented before materialization."
            )

        invalid_nonzero = _scalar(
            connection,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{ROUTE_TABLE}] r
            LEFT JOIN [{target_schema}].[concept] c
              ON c.concept_id = r.target_concept_id
            WHERE r.target_domain = :domain
              AND r.target_concept_id <> 0
              AND (
                   c.concept_id IS NULL
                OR c.standard_concept <> 'S'
                OR c.invalid_reason IS NOT NULL
                OR c.domain_id <> :domain
              )
            """,
            {"domain": domain},
        )
        if invalid_nonzero:
            raise RuntimeError(
                f"{domain} route ledger contains {invalid_nonzero:,} nonzero target "
                "concept(s) that are not active Standard concepts in that domain"
            )
        route_counts[domain] = rows

    person_unlinked = _scalar(
        connection,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{target_schema}].[{ROUTE_TABLE}] r
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
    return route_counts


def _count_condition_procedure_xwalk(connection, target_schema: str) -> int:
    return _scalar(
        connection,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{target_schema}].[{CONDITION_XWALK}]
        WHERE source_domain = 'PROCEDURES'
        """,
    )


def _existing_state(connection, target_schema: str) -> dict[str, int | bool]:
    device_xwalk_exists = table_exists(connection, target_schema, DEVICE_XWALK)
    specimen_xwalk_exists = table_exists(connection, target_schema, SPECIMEN_XWALK)
    return {
        "condition_rows": _scalar(
            connection,
            f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]",
        ),
        "condition_xwalk_rows": _scalar(
            connection,
            f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{CONDITION_XWALK}]",
        ),
        "condition_procedure_xwalk": _count_condition_procedure_xwalk(
            connection, target_schema
        ),
        "device_rows": _scalar(
            connection,
            f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure]",
        ),
        "specimen_rows": _scalar(
            connection,
            f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen]",
        ),
        "device_xwalk_exists": device_xwalk_exists,
        "specimen_xwalk_exists": specimen_xwalk_exists,
        "device_xwalk_rows": (
            _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{DEVICE_XWALK}]",
            )
            if device_xwalk_exists
            else 0
        ),
        "specimen_xwalk_rows": (
            _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{SPECIMEN_XWALK}]",
            )
            if specimen_xwalk_exists
            else 0
        ),
    }


def _materialized_mismatch_rows(
    connection,
    target_schema: str,
    domain: str,
) -> int:
    if domain == "Condition":
        return _scalar(
            connection,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{CONDITION_XWALK}] x
            JOIN [{target_schema}].[condition_occurrence] c
              ON c.condition_occurrence_id = x.condition_occurrence_id
            LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
              ON r.source_procedure_id = x.source_record_id
             AND r.target_domain = 'Condition'
            WHERE x.source_domain = 'PROCEDURES'
              AND (
                   r.source_procedure_id IS NULL
                OR c.condition_concept_id <> r.target_concept_id
                OR c.condition_type_concept_id <> 0
              )
            """,
        )
    if domain == "Device":
        return _scalar(
            connection,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{DEVICE_XWALK}] x
            JOIN [{target_schema}].[device_exposure] d
              ON d.device_exposure_id = x.device_exposure_id
            LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
              ON r.source_procedure_id = x.source_record_id
             AND r.target_domain = 'Device'
            WHERE r.source_procedure_id IS NULL
               OR d.device_concept_id <> r.target_concept_id
               OR d.device_type_concept_id <> 0
            """,
        )
    return _scalar(
        connection,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{target_schema}].[{SPECIMEN_XWALK}] x
        JOIN [{target_schema}].[specimen] s
          ON s.specimen_id = x.specimen_id
        LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
          ON r.source_procedure_id = x.source_record_id
         AND r.target_domain = 'Specimen'
        WHERE r.source_procedure_id IS NULL
           OR s.specimen_concept_id <> r.target_concept_id
           OR s.specimen_type_concept_id <> 0
        """,
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
            route_counts = _validate_routes(connection, target_schema)
            state = _existing_state(connection, target_schema)

            already_matched = (
                state["condition_procedure_xwalk"] == route_counts["Condition"]
                and state["device_rows"] == route_counts["Device"]
                and state["device_xwalk_rows"] == route_counts["Device"]
                and state["specimen_rows"] == route_counts["Specimen"]
                and state["specimen_xwalk_rows"] == route_counts["Specimen"]
            )
            if already_matched:
                mismatches = {
                    domain: _materialized_mismatch_rows(
                        connection, target_schema, domain
                    )
                    for domain in DOMAINS
                }
                if any(mismatches.values()):
                    raise RuntimeError(
                        "Existing remaining-domain materialization does not match "
                        f"current route/type policy: {mismatches}"
                    )
                status = "already_matched"
                baseline_condition_rows = (
                    int(state["condition_rows"]) - route_counts["Condition"]
                )
            else:
                pristine = (
                    state["condition_xwalk_rows"] == state["condition_rows"]
                    and state["condition_procedure_xwalk"] == 0
                    and state["device_rows"] == 0
                    and state["specimen_rows"] == 0
                    and not state["device_xwalk_exists"]
                    and not state["specimen_xwalk_exists"]
                )
                if not pristine:
                    raise RuntimeError(
                        "Remaining-domain targets are neither pristine nor a matching "
                        f"materialization: {state}"
                    )

                baseline_condition_rows = int(state["condition_rows"])
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
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value = r.patid
                          LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                            ON v.encounterid = r.encounterid
                          WHERE r.target_domain = 'Condition'
                        )
                        INSERT INTO [{target_schema}].[condition_occurrence] (
                          condition_occurrence_id, person_id, condition_concept_id,
                          condition_start_date, condition_start_datetime,
                          condition_end_date, condition_end_datetime,
                          condition_type_concept_id, condition_status_concept_id,
                          stop_reason, provider_id, visit_occurrence_id,
                          visit_detail_id, condition_source_value,
                          condition_source_concept_id,
                          condition_status_source_value
                        )
                        SELECT
                          :base_id + CONVERT(bigint, rn), person_id,
                          CONVERT(int, target_concept_id), px_date,
                          CAST(px_date AS datetime2(7)), NULL, NULL,
                          0, 0, NULL, NULL, visit_occurrence_id, NULL,
                          LEFT(CONVERT(varchar(50), px), 50),
                          CONVERT(int, source_concept_id), NULL
                        FROM src
                        """
                    ),
                    {"base_id": condition_max_id},
                )
                connection.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT
                            ROW_NUMBER() OVER (
                              ORDER BY source_procedure_id, target_concept_id
                            ) AS rn,
                            *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain = 'Condition'
                        )
                        INSERT INTO [{target_schema}].[{CONDITION_XWALK}] (
                          source_domain, source_record_id, condition_occurrence_id,
                          source_code_type, source_provenance, date_basis
                        )
                        SELECT
                          'PROCEDURES', source_procedure_id,
                          :base_id + CONVERT(bigint, rn),
                          LEFT(CONVERT(nvarchar(50), px_type), 50),
                          LEFT(CONVERT(nvarchar(50), px_source), 50), 'PX_DATE'
                        FROM src
                        """
                    ),
                    {"base_id": condition_max_id},
                )

                connection.execute(
                    text(
                        f"""
                        CREATE TABLE [{target_schema}].[{DEVICE_XWALK}] (
                          source_record_id nvarchar(255) NOT NULL PRIMARY KEY,
                          device_exposure_id bigint NOT NULL UNIQUE,
                          source_code_type nvarchar(50) NULL,
                          source_provenance nvarchar(50) NULL,
                          date_basis varchar(32) NOT NULL
                        )
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
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value = r.patid
                          LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                            ON v.encounterid = r.encounterid
                          WHERE r.target_domain = 'Device'
                        )
                        INSERT INTO [{target_schema}].[device_exposure] (
                          device_exposure_id, person_id, device_concept_id,
                          device_exposure_start_date,
                          device_exposure_start_datetime,
                          device_exposure_end_date, device_exposure_end_datetime,
                          device_type_concept_id, unique_device_id, production_id,
                          quantity, provider_id, visit_occurrence_id,
                          visit_detail_id, device_source_value,
                          device_source_concept_id, unit_concept_id,
                          unit_source_value, unit_source_concept_id
                        )
                        SELECT
                          CONVERT(bigint, device_exposure_id), person_id,
                          CONVERT(int, target_concept_id), px_date,
                          CAST(px_date AS datetime2(7)), NULL, NULL,
                          0, NULL, NULL, NULL, NULL, visit_occurrence_id, NULL,
                          LEFT(CONVERT(varchar(50), px), 50),
                          CONVERT(int, source_concept_id), NULL, NULL, NULL
                        FROM src
                        """
                    )
                )
                connection.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT
                            ROW_NUMBER() OVER (
                              ORDER BY source_procedure_id, target_concept_id
                            ) AS device_exposure_id,
                            *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain = 'Device'
                        )
                        INSERT INTO [{target_schema}].[{DEVICE_XWALK}] (
                          source_record_id, device_exposure_id,
                          source_code_type, source_provenance, date_basis
                        )
                        SELECT
                          source_procedure_id,
                          CONVERT(bigint, device_exposure_id),
                          LEFT(CONVERT(nvarchar(50), px_type), 50),
                          LEFT(CONVERT(nvarchar(50), px_source), 50), 'PX_DATE'
                        FROM src
                        """
                    )
                )

                connection.execute(
                    text(
                        f"""
                        CREATE TABLE [{target_schema}].[{SPECIMEN_XWALK}] (
                          source_record_id nvarchar(255) NOT NULL PRIMARY KEY,
                          specimen_id bigint NOT NULL UNIQUE,
                          source_code_type nvarchar(50) NULL,
                          source_provenance nvarchar(50) NULL,
                          date_basis varchar(32) NOT NULL
                        )
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
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value = r.patid
                          WHERE r.target_domain = 'Specimen'
                        )
                        INSERT INTO [{target_schema}].[specimen] (
                          specimen_id, person_id, specimen_concept_id,
                          specimen_type_concept_id, specimen_date,
                          specimen_datetime, quantity, unit_concept_id,
                          anatomic_site_concept_id, disease_status_concept_id,
                          specimen_source_id, specimen_source_value,
                          unit_source_value, anatomic_site_source_value,
                          disease_status_source_value
                        )
                        SELECT
                          CONVERT(bigint, specimen_id), person_id,
                          CONVERT(int, target_concept_id), 0, px_date,
                          CAST(px_date AS datetime2(7)), NULL, NULL, NULL, NULL,
                          LEFT(CONVERT(varchar(50), source_procedure_id), 50),
                          LEFT(CONVERT(varchar(50), px), 50), NULL, NULL, NULL
                        FROM src
                        """
                    )
                )
                connection.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT
                            ROW_NUMBER() OVER (
                              ORDER BY source_procedure_id, target_concept_id
                            ) AS specimen_id,
                            *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain = 'Specimen'
                        )
                        INSERT INTO [{target_schema}].[{SPECIMEN_XWALK}] (
                          source_record_id, specimen_id, source_code_type,
                          source_provenance, date_basis
                        )
                        SELECT
                          source_procedure_id, CONVERT(bigint, specimen_id),
                          LEFT(CONVERT(nvarchar(50), px_type), 50),
                          LEFT(CONVERT(nvarchar(50), px_source), 50), 'PX_DATE'
                        FROM src
                        """
                    )
                )
                status = "matched"

            final_state = _existing_state(connection, target_schema)
            expected_condition_rows = (
                baseline_condition_rows + route_counts["Condition"]
            )
            checks = {
                "condition_total": (
                    int(final_state["condition_rows"]), expected_condition_rows
                ),
                "condition_lineage": (
                    int(final_state["condition_xwalk_rows"]),
                    expected_condition_rows,
                ),
                "condition_procedure_lineage": (
                    int(final_state["condition_procedure_xwalk"]),
                    route_counts["Condition"],
                ),
                "device": (
                    int(final_state["device_rows"]), route_counts["Device"]
                ),
                "device_lineage": (
                    int(final_state["device_xwalk_rows"]), route_counts["Device"]
                ),
                "specimen": (
                    int(final_state["specimen_rows"]), route_counts["Specimen"]
                ),
                "specimen_lineage": (
                    int(final_state["specimen_xwalk_rows"]), route_counts["Specimen"]
                ),
            }
            failed = {key: value for key, value in checks.items() if value[0] != value[1]}
            if failed:
                raise RuntimeError(
                    f"Remaining Procedure-domain reconciliation failed: {failed}"
                )

            mismatches = {
                domain: _materialized_mismatch_rows(
                    connection, target_schema, domain
                )
                for domain in DOMAINS
            }
            if any(mismatches.values()):
                raise RuntimeError(
                    f"Remaining Procedure-domain row-level mismatch: {mismatches}"
                )

            concept_zero = {
                "Condition": _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] c
                    JOIN [{target_schema}].[{CONDITION_XWALK}] x
                      ON x.condition_occurrence_id = c.condition_occurrence_id
                    WHERE x.source_domain = 'PROCEDURES'
                      AND c.condition_concept_id = 0
                    """,
                ),
                "Device": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure] "
                    "WHERE device_concept_id = 0",
                ),
                "Specimen": _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen] "
                    "WHERE specimen_concept_id = 0",
                ),
            }

        payload = {
            "stage": "procedure_remaining_domains",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "baseline_condition_rows": baseline_condition_rows,
            "route_rows": route_counts,
            "condition_rows": int(final_state["condition_rows"]),
            "condition_procedure_rows": int(
                final_state["condition_procedure_xwalk"]
            ),
            "device_rows": int(final_state["device_rows"]),
            "specimen_rows": int(final_state["specimen_rows"]),
            "concept_zero_rows": concept_zero,
            "type_concept_policy": (
                "Use concept_id 0 for Condition, Device, and Specimen type fields "
                "because PCORnet PROCEDURES table membership and PX_SOURCE do not, "
                "by themselves, establish an exact OMOP type concept. Source "
                "provenance remains in lineage."
            ),
            "lineage_policy": (
                "ETL route and xwalk tables live in target_schema; source_schema "
                "contains only PCORnet staging tables."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
