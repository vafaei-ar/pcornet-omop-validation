from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


DOMAINS = ("Condition", "Device", "Specimen")
ROUTE_TABLE = "etl_procedure_event_route"
PRIMARY_CONDITION_XWALK = "etl_condition_occurrence_xwalk"
OBSCLIN_CONDITION_XWALK = "etl_obs_clin_condition_xwalk"
PROCEDURE_CONDITION_XWALK = "etl_procedure_condition_xwalk"
DEVICE_XWALK = "etl_device_exposure_xwalk"
SPECIMEN_XWALK = "etl_specimen_xwalk"


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def _xwalk_rows(con, schema: str, table: str) -> int:
    if not table_exists(con, schema, table):
        return 0
    return _scalar(con, f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")


def _require_tables(con, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_PROCEDURES"),
        (target_schema, ROUTE_TABLE),
        (target_schema, PRIMARY_CONDITION_XWALK),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "person"),
        (target_schema, "condition_occurrence"),
        (target_schema, "device_exposure"),
        (target_schema, "specimen"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(con, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _route_profiles(con, target_schema: str) -> tuple[dict[str, int], dict[str, int]]:
    route_counts: dict[str, int] = {}
    distinct_counts: dict[str, int] = {}
    for domain in DOMAINS:
        route_counts[domain] = _scalar(
            con,
            f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}] "
            "WHERE target_domain=:domain",
            {"domain": domain},
        )
        distinct_counts[domain] = _scalar(
            con,
            f"SELECT COUNT_BIG(DISTINCT source_procedure_id) "
            f"FROM [{target_schema}].[{ROUTE_TABLE}] WHERE target_domain=:domain",
            {"domain": domain},
        )
        invalid = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{ROUTE_TABLE}] r
            LEFT JOIN [{target_schema}].[concept] c
              ON c.concept_id=r.target_concept_id
            WHERE r.target_domain=:domain
              AND r.target_concept_id<>0
              AND (
                   c.concept_id IS NULL
                OR c.domain_id<>:domain
                OR c.standard_concept<>'S'
                OR c.invalid_reason IS NOT NULL
              )
            """,
            {"domain": domain},
        )
        if invalid:
            raise RuntimeError(
                f"{domain} routes contain {invalid:,} invalid nonzero Standard targets"
            )

    unlinked = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{target_schema}].[{ROUTE_TABLE}] r
        LEFT JOIN [{target_schema}].[person] p
          ON p.person_source_value=r.patid
        WHERE r.target_domain IN ('Condition','Device','Specimen')
          AND p.person_id IS NULL
        """,
    )
    if unlinked:
        raise RuntimeError(f"Remaining Procedure routes have {unlinked:,} unlinked persons")
    return route_counts, distinct_counts


def _state(con, target_schema: str) -> dict[str, int | bool]:
    return {
        "condition_rows": _scalar(
            con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[condition_occurrence]"
        ),
        "primary_condition_xwalk_rows": _xwalk_rows(
            con, target_schema, PRIMARY_CONDITION_XWALK
        ),
        "obsclin_condition_xwalk_rows": _xwalk_rows(
            con, target_schema, OBSCLIN_CONDITION_XWALK
        ),
        "procedure_condition_xwalk_exists": table_exists(
            con, target_schema, PROCEDURE_CONDITION_XWALK
        ),
        "procedure_condition_xwalk_rows": _xwalk_rows(
            con, target_schema, PROCEDURE_CONDITION_XWALK
        ),
        "device_rows": _scalar(
            con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure]"
        ),
        "specimen_rows": _scalar(
            con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen]"
        ),
        "device_xwalk_exists": table_exists(con, target_schema, DEVICE_XWALK),
        "device_xwalk_rows": _xwalk_rows(con, target_schema, DEVICE_XWALK),
        "specimen_xwalk_exists": table_exists(con, target_schema, SPECIMEN_XWALK),
        "specimen_xwalk_rows": _xwalk_rows(con, target_schema, SPECIMEN_XWALK),
    }


def _create_xwalks(con, target_schema: str) -> None:
    for table, id_name in (
        (PROCEDURE_CONDITION_XWALK, "condition_occurrence_id"),
        (DEVICE_XWALK, "device_exposure_id"),
        (SPECIMEN_XWALK, "specimen_id"),
    ):
        con.exec_driver_sql(
            f"""
            CREATE TABLE [{target_schema}].[{table}] (
              route_id bigint NOT NULL PRIMARY KEY,
              source_record_id nvarchar(255) NOT NULL,
              {id_name} bigint NOT NULL UNIQUE,
              target_concept_id bigint NOT NULL,
              route_status varchar(64) NOT NULL,
              source_code_type nvarchar(50) NULL,
              source_provenance nvarchar(50) NULL,
              date_basis varchar(32) NOT NULL
            )
            """
        )


def _mismatch_rows(con, target_schema: str, domain: str) -> int:
    if domain == "Condition":
        return _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{PROCEDURE_CONDITION_XWALK}] x
            LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
              ON r.route_id=x.route_id AND r.target_domain='Condition'
            LEFT JOIN [{target_schema}].[condition_occurrence] c
              ON c.condition_occurrence_id=x.condition_occurrence_id
            WHERE r.route_id IS NULL OR c.condition_occurrence_id IS NULL
               OR x.source_record_id<>r.source_procedure_id
               OR x.target_concept_id<>r.target_concept_id
               OR c.condition_concept_id<>r.target_concept_id
               OR c.condition_type_concept_id<>0
            """,
        )
    if domain == "Device":
        return _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{target_schema}].[{DEVICE_XWALK}] x
            LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
              ON r.route_id=x.route_id AND r.target_domain='Device'
            LEFT JOIN [{target_schema}].[device_exposure] d
              ON d.device_exposure_id=x.device_exposure_id
            WHERE r.route_id IS NULL OR d.device_exposure_id IS NULL
               OR x.source_record_id<>r.source_procedure_id
               OR x.target_concept_id<>r.target_concept_id
               OR d.device_concept_id<>r.target_concept_id
               OR d.device_type_concept_id<>0
            """,
        )
    return _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM [{target_schema}].[{SPECIMEN_XWALK}] x
        LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r
          ON r.route_id=x.route_id AND r.target_domain='Specimen'
        LEFT JOIN [{target_schema}].[specimen] s
          ON s.specimen_id=x.specimen_id
        WHERE r.route_id IS NULL OR s.specimen_id IS NULL
           OR x.source_record_id<>r.source_procedure_id
           OR x.target_concept_id<>r.target_concept_id
           OR s.specimen_concept_id<>r.target_concept_id
           OR s.specimen_type_concept_id<>0
        """,
    )


def materialize_procedure_remaining_domains(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "procedure_remaining_domains.json"
    engine = make_engine(config)

    try:
        with engine.begin() as con:
            _require_tables(con, source_schema, target_schema)
            route_counts, distinct_counts = _route_profiles(con, target_schema)
            state = _state(con, target_schema)

            prior_condition_lineage = (
                int(state["primary_condition_xwalk_rows"])
                + int(state["obsclin_condition_xwalk_rows"])
            )

            already_matched = (
                state["procedure_condition_xwalk_rows"] == route_counts["Condition"]
                and state["device_xwalk_rows"] == route_counts["Device"]
                and state["specimen_xwalk_rows"] == route_counts["Specimen"]
                and state["device_rows"] == route_counts["Device"]
                and state["specimen_rows"] == route_counts["Specimen"]
                and int(state["condition_rows"])
                    == prior_condition_lineage + route_counts["Condition"]
            )

            if already_matched:
                baseline_condition_rows = prior_condition_lineage
                status = "already_matched"
            else:
                pristine = (
                    int(state["condition_rows"]) == prior_condition_lineage
                    and not state["procedure_condition_xwalk_exists"]
                    and int(state["device_rows"]) == 0
                    and int(state["specimen_rows"]) == 0
                    and not state["device_xwalk_exists"]
                    and not state["specimen_xwalk_exists"]
                )
                if not pristine:
                    raise RuntimeError(
                        "Remaining-domain targets are neither pristine nor a matching "
                        f"route-aware materialization: {state}"
                    )

                baseline_condition_rows = int(state["condition_rows"])
                condition_base = _scalar(
                    con,
                    f"SELECT COALESCE(MAX(condition_occurrence_id),0) "
                    f"FROM [{target_schema}].[condition_occurrence]",
                )
                device_base = _scalar(
                    con,
                    f"SELECT COALESCE(MAX(device_exposure_id),0) "
                    f"FROM [{target_schema}].[device_exposure]",
                )
                specimen_base = _scalar(
                    con,
                    f"SELECT COALESCE(MAX(specimen_id),0) FROM [{target_schema}].[specimen]",
                )
                _create_xwalks(con, target_schema)

                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY r.route_id) rn,
                                 r.*, p.person_id, v.visit_occurrence_id
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value=r.patid
                          LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                            ON v.encounterid=r.encounterid
                          WHERE r.target_domain='Condition'
                        )
                        INSERT INTO [{target_schema}].[condition_occurrence] (
                          condition_occurrence_id, person_id, condition_concept_id,
                          condition_start_date, condition_start_datetime,
                          condition_end_date, condition_end_datetime,
                          condition_type_concept_id, condition_status_concept_id,
                          stop_reason, provider_id, visit_occurrence_id, visit_detail_id,
                          condition_source_value, condition_source_concept_id,
                          condition_status_source_value
                        )
                        SELECT :base+CONVERT(bigint,rn), person_id,
                               CONVERT(int,target_concept_id), px_date,
                               CAST(px_date AS datetime2(7)), NULL, NULL,
                               0, 0, NULL, NULL, visit_occurrence_id, NULL,
                               LEFT(CONVERT(varchar(50),px),50),
                               CONVERT(int,source_concept_id), NULL
                        FROM src
                        """
                    ),
                    {"base": condition_base},
                )
                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY route_id) rn, *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain='Condition'
                        )
                        INSERT INTO [{target_schema}].[{PROCEDURE_CONDITION_XWALK}] (
                          route_id, source_record_id, condition_occurrence_id,
                          target_concept_id, route_status, source_code_type,
                          source_provenance, date_basis
                        )
                        SELECT route_id, source_procedure_id,
                               :base+CONVERT(bigint,rn), target_concept_id,
                               route_status, LEFT(CONVERT(nvarchar(50),px_type),50),
                               LEFT(CONVERT(nvarchar(50),px_source),50), 'PX_DATE'
                        FROM src
                        """
                    ),
                    {"base": condition_base},
                )

                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY r.route_id) rn,
                                 r.*, p.person_id, v.visit_occurrence_id
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value=r.patid
                          LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                            ON v.encounterid=r.encounterid
                          WHERE r.target_domain='Device'
                        )
                        INSERT INTO [{target_schema}].[device_exposure] (
                          device_exposure_id, person_id, device_concept_id,
                          device_exposure_start_date, device_exposure_start_datetime,
                          device_exposure_end_date, device_exposure_end_datetime,
                          device_type_concept_id, unique_device_id, production_id,
                          quantity, provider_id, visit_occurrence_id, visit_detail_id,
                          device_source_value, device_source_concept_id,
                          unit_concept_id, unit_source_value, unit_source_concept_id
                        )
                        SELECT :base+CONVERT(bigint,rn), person_id,
                               CONVERT(int,target_concept_id), px_date,
                               CAST(px_date AS datetime2(7)), NULL, NULL,
                               0, NULL, NULL, NULL, NULL, visit_occurrence_id, NULL,
                               LEFT(CONVERT(varchar(50),px),50),
                               CONVERT(int,source_concept_id), 0, NULL, 0
                        FROM src
                        """
                    ),
                    {"base": device_base},
                )
                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY route_id) rn, *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain='Device'
                        )
                        INSERT INTO [{target_schema}].[{DEVICE_XWALK}] (
                          route_id, source_record_id, device_exposure_id,
                          target_concept_id, route_status, source_code_type,
                          source_provenance, date_basis
                        )
                        SELECT route_id, source_procedure_id,
                               :base+CONVERT(bigint,rn), target_concept_id,
                               route_status, LEFT(CONVERT(nvarchar(50),px_type),50),
                               LEFT(CONVERT(nvarchar(50),px_source),50), 'PX_DATE'
                        FROM src
                        """
                    ),
                    {"base": device_base},
                )

                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY r.route_id) rn,
                                 r.*, p.person_id
                          FROM [{target_schema}].[{ROUTE_TABLE}] r
                          JOIN [{target_schema}].[person] p
                            ON p.person_source_value=r.patid
                          WHERE r.target_domain='Specimen'
                        )
                        INSERT INTO [{target_schema}].[specimen] (
                          specimen_id, person_id, specimen_concept_id,
                          specimen_type_concept_id, specimen_date, specimen_datetime,
                          quantity, unit_concept_id, anatomic_site_concept_id,
                          disease_status_concept_id, specimen_source_id,
                          specimen_source_value, unit_source_value,
                          anatomic_site_source_value, disease_status_source_value
                        )
                        SELECT :base+CONVERT(bigint,rn), person_id,
                               CONVERT(int,target_concept_id), 0, px_date,
                               CAST(px_date AS datetime2(7)), NULL, 0, 0, 0,
                               LEFT(CONVERT(varchar(50),source_procedure_id),50),
                               LEFT(CONVERT(varchar(50),px),50), NULL, NULL, NULL
                        FROM src
                        """
                    ),
                    {"base": specimen_base},
                )
                con.execute(
                    text(
                        f"""
                        WITH src AS (
                          SELECT ROW_NUMBER() OVER (ORDER BY route_id) rn, *
                          FROM [{target_schema}].[{ROUTE_TABLE}]
                          WHERE target_domain='Specimen'
                        )
                        INSERT INTO [{target_schema}].[{SPECIMEN_XWALK}] (
                          route_id, source_record_id, specimen_id,
                          target_concept_id, route_status, source_code_type,
                          source_provenance, date_basis
                        )
                        SELECT route_id, source_procedure_id,
                               :base+CONVERT(bigint,rn), target_concept_id,
                               route_status, LEFT(CONVERT(nvarchar(50),px_type),50),
                               LEFT(CONVERT(nvarchar(50),px_source),50), 'PX_DATE'
                        FROM src
                        """
                    ),
                    {"base": specimen_base},
                )
                status = "matched"

            final = _state(con, target_schema)
            expected_condition_rows = baseline_condition_rows + route_counts["Condition"]
            expected_condition_lineage = (
                int(final["primary_condition_xwalk_rows"])
                + int(final["obsclin_condition_xwalk_rows"])
                + int(final["procedure_condition_xwalk_rows"])
            )
            checks = {
                "condition_total": (int(final["condition_rows"]), expected_condition_rows),
                "condition_total_lineage": (
                    expected_condition_lineage, int(final["condition_rows"])
                ),
                "condition_procedure_lineage": (
                    int(final["procedure_condition_xwalk_rows"]), route_counts["Condition"]
                ),
                "device": (int(final["device_rows"]), route_counts["Device"]),
                "device_lineage": (
                    int(final["device_xwalk_rows"]), route_counts["Device"]
                ),
                "specimen": (int(final["specimen_rows"]), route_counts["Specimen"]),
                "specimen_lineage": (
                    int(final["specimen_xwalk_rows"]), route_counts["Specimen"]
                ),
            }
            failed = {k: v for k, v in checks.items() if v[0] != v[1]}
            if failed:
                raise RuntimeError(f"Remaining Procedure-domain reconciliation failed: {failed}")

            mismatches = {d: _mismatch_rows(con, target_schema, d) for d in DOMAINS}
            if any(mismatches.values()):
                raise RuntimeError(f"Remaining Procedure-domain row mismatch: {mismatches}")

            concept_zero = {
                "Condition": _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[condition_occurrence] c
                    JOIN [{target_schema}].[{PROCEDURE_CONDITION_XWALK}] x
                      ON x.condition_occurrence_id=c.condition_occurrence_id
                    WHERE c.condition_concept_id=0
                    """,
                ),
                "Device": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[device_exposure] "
                    "WHERE device_concept_id=0",
                ),
                "Specimen": _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[specimen] "
                    "WHERE specimen_concept_id=0",
                ),
            }

        payload = {
            "stage": "procedure_remaining_domains",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "baseline_condition_rows": baseline_condition_rows,
            "prior_condition_lineage_rows": prior_condition_lineage,
            "route_rows": route_counts,
            "distinct_source_events": distinct_counts,
            "one_to_many_expansion": {
                d: route_counts[d] - distinct_counts[d] for d in DOMAINS
            },
            "condition_rows": int(final["condition_rows"]),
            "condition_procedure_rows": int(final["procedure_condition_xwalk_rows"]),
            "device_rows": int(final["device_rows"]),
            "specimen_rows": int(final["specimen_rows"]),
            "concept_zero_rows": concept_zero,
            "type_concept_policy": (
                "Use concept_id 0 for Condition, Device, and Specimen type fields because "
                "PCORnet PROCEDURES membership and PX_SOURCE do not establish an exact OMOP type."
            ),
            "lineage_policy": (
                "Procedure-derived rows use route-aware lineage. Condition reconciliation "
                "explicitly aggregates previously materialized canonical and OBS_CLIN Condition "
                "lineage before adding the independent Procedure route namespace."
            ),
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
