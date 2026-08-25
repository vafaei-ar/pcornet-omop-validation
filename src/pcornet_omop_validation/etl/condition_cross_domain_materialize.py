from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .condition_occurrence import _eligible_ctes
from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_condition_event_route_v2"
XWALK_TABLE = "etl_condition_cross_domain_xwalk"
DOMAINS = ("Observation", "Procedure", "Measurement", "Drug", "Device", "Specimen")
TARGETS = {
    "Observation": ("observation", "observation_id", "observation_concept_id"),
    "Procedure": ("procedure_occurrence", "procedure_occurrence_id", "procedure_concept_id"),
    "Measurement": ("measurement", "measurement_id", "measurement_concept_id"),
    "Drug": ("drug_exposure", "drug_exposure_id", "drug_concept_id"),
    "Device": ("device_exposure", "device_exposure_id", "device_concept_id"),
    "Specimen": ("specimen", "specimen_id", "specimen_concept_id"),
}
BASE_LINEAGE = {
    "Observation": ("etl_observation_xwalk",),
    "Procedure": ("etl_procedure_occurrence_xwalk",),
    "Measurement": ("etl_measurement_xwalk",),
    "Drug": ("etl_drug_exposure_xwalk",),
    "Device": ("etl_device_exposure_xwalk",),
    "Specimen": ("etl_specimen_xwalk",),
}


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _events_cte(source_schema: str, target_schema: str) -> str:
    eligible = _eligible_ctes(source_schema, target_schema)
    return eligible + """
    , events AS (
      SELECT
        CAST('DIAGNOSIS' AS varchar(16)) AS source_domain,
        CAST(d.DIAGNOSISID AS nvarchar(255)) AS source_record_id,
        d.person_id,
        d.visit_occurrence_id,
        CAST(d.DX_DATE AS date) AS event_date,
        CAST(CAST(d.DX_DATE AS date) AS datetime2(7)) AS event_datetime,
        CAST(d.DX AS nvarchar(255)) AS source_value
      FROM diag_eligible d
      UNION ALL
      SELECT
        CAST('CONDITION' AS varchar(16)),
        CAST(c.CONDITIONID AS nvarchar(255)),
        c.person_id,
        c.visit_occurrence_id,
        CAST(c.effective_start_date AS date),
        CAST(CAST(c.effective_start_date AS date) AS datetime2(7)),
        CAST(c.CONDITION AS nvarchar(255))
      FROM cond_eligible c
    )
    """


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = [
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_CONDITION"),
        (target_schema, ROUTE_TABLE),
        (target_schema, "person"),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "concept"),
    ]
    for table, _, _ in TARGETS.values():
        required.append((target_schema, table))
    for lineage_tables in BASE_LINEAGE.values():
        for table in lineage_tables:
            required.append((target_schema, table))
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def _route_counts(connection, target_schema: str) -> dict[str, int]:
    rows = connection.execute(
        text(
            f"""
            SELECT target_domain, COUNT_BIG(*)
            FROM [{target_schema}].[{ROUTE_TABLE}]
            WHERE is_core_event_route=1
              AND target_domain IN ('Observation','Procedure','Measurement','Drug','Device','Specimen')
            GROUP BY target_domain
            """
        )
    ).fetchall()
    counts = {domain: 0 for domain in DOMAINS}
    counts.update({str(row[0]): int(row[1]) for row in rows})
    return counts


def _base_lineage_count(connection, target_schema: str, domain: str) -> int:
    return sum(
        _scalar(connection, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]")
        for table in BASE_LINEAGE[domain]
    )


def _insert_xwalk_domain(connection, target_schema: str, domain: str, base_id: int) -> None:
    connection.execute(
        text(
            f"""
            WITH src AS (
              SELECT
                ROW_NUMBER() OVER (ORDER BY route_id) AS rn,
                route_id, source_domain, source_record_id,
                source_concept_id, target_concept_id, route_status
              FROM [{target_schema}].[{ROUTE_TABLE}]
              WHERE is_core_event_route=1 AND target_domain=:domain
            )
            INSERT INTO [{target_schema}].[{XWALK_TABLE}] (
              route_id, target_domain, target_row_id, source_domain,
              source_record_id, source_concept_id, target_concept_id, route_status
            )
            SELECT
              route_id, :domain, :base_id + CONVERT(bigint,rn), source_domain,
              source_record_id, source_concept_id, target_concept_id, route_status
            FROM src
            """
        ),
        {"domain": domain, "base_id": base_id},
    )


def _insert_domain_rows(
    connection,
    source_schema: str,
    target_schema: str,
    domain: str,
) -> None:
    cte = _events_cte(source_schema, target_schema)
    x = f"[{target_schema}].[{XWALK_TABLE}]"
    if domain == "Observation":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[observation] (
          observation_id, person_id, observation_concept_id,
          observation_date, observation_datetime, observation_type_concept_id,
          value_as_number, value_as_string, value_as_concept_id,
          qualifier_concept_id, unit_concept_id, provider_id,
          visit_occurrence_id, visit_detail_id, observation_source_value,
          observation_source_concept_id, unit_source_value, qualifier_source_value
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          e.event_date, e.event_datetime, 0,
          NULL, NULL, 0, 0, 0, NULL,
          e.visit_occurrence_id, NULL, LEFT(CONVERT(varchar(50),e.source_value),50),
          CONVERT(int,x.source_concept_id), NULL, NULL
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Observation'
        """
    elif domain == "Procedure":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[procedure_occurrence] (
          procedure_occurrence_id, person_id, procedure_concept_id,
          procedure_date, procedure_datetime, procedure_end_date,
          procedure_end_datetime, procedure_type_concept_id, modifier_concept_id,
          quantity, provider_id, visit_occurrence_id, visit_detail_id,
          procedure_source_value, procedure_source_concept_id, modifier_source_value
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          e.event_date, e.event_datetime, NULL, NULL, 0, 0,
          NULL, NULL, e.visit_occurrence_id, NULL,
          LEFT(CONVERT(varchar(50),e.source_value),50),
          CONVERT(int,x.source_concept_id), NULL
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Procedure'
        """
    elif domain == "Measurement":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[measurement] (
          measurement_id, person_id, measurement_concept_id,
          measurement_date, measurement_datetime, measurement_time,
          measurement_type_concept_id, operator_concept_id, value_as_number,
          value_as_concept_id, unit_concept_id, range_low, range_high,
          provider_id, visit_occurrence_id, visit_detail_id,
          measurement_source_value, measurement_source_concept_id,
          unit_source_value, unit_source_concept_id, value_source_value,
          measurement_event_id, meas_event_field_concept_id
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          e.event_date, e.event_datetime, NULL,
          0, 0, NULL, 0, 0, NULL, NULL,
          NULL, e.visit_occurrence_id, NULL,
          LEFT(CONVERT(varchar(50),e.source_value),50),
          CONVERT(int,x.source_concept_id), NULL, 0, NULL, NULL, 0
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Measurement'
        """
    elif domain == "Drug":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[drug_exposure] (
          drug_exposure_id, person_id, drug_concept_id,
          drug_exposure_start_date, drug_exposure_start_datetime,
          drug_exposure_end_date, drug_exposure_end_datetime,
          verbatim_end_date, drug_type_concept_id, stop_reason,
          refills, quantity, days_supply, sig, route_concept_id,
          lot_number, provider_id, visit_occurrence_id, visit_detail_id,
          drug_source_value, drug_source_concept_id,
          route_source_value, dose_unit_source_value
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          e.event_date, e.event_datetime, e.event_date, e.event_datetime,
          e.event_date, 0, NULL,
          NULL, NULL, NULL, NULL, 0,
          NULL, NULL, e.visit_occurrence_id, NULL,
          LEFT(CONVERT(varchar(50),e.source_value),50),
          CONVERT(int,x.source_concept_id), NULL, NULL
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Drug'
        """
    elif domain == "Device":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[device_exposure] (
          device_exposure_id, person_id, device_concept_id,
          device_exposure_start_date, device_exposure_start_datetime,
          device_exposure_end_date, device_exposure_end_datetime,
          device_type_concept_id, unique_device_id, production_id,
          quantity, provider_id, visit_occurrence_id, visit_detail_id,
          device_source_value, device_source_concept_id,
          unit_concept_id, unit_source_value, unit_source_concept_id
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          e.event_date, e.event_datetime, NULL, NULL,
          0, NULL, NULL, NULL, NULL, e.visit_occurrence_id, NULL,
          LEFT(CONVERT(varchar(50),e.source_value),50),
          CONVERT(int,x.source_concept_id), 0, NULL, 0
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Device'
        """
    elif domain == "Specimen":
        sql = cte + f"""
        INSERT INTO [{target_schema}].[specimen] (
          specimen_id, person_id, specimen_concept_id,
          specimen_type_concept_id, specimen_date, specimen_datetime,
          quantity, unit_concept_id, anatomic_site_concept_id,
          disease_status_concept_id, specimen_source_id, specimen_source_value,
          unit_source_value, anatomic_site_source_value, disease_status_source_value
        )
        SELECT
          x.target_row_id, e.person_id, CONVERT(int,x.target_concept_id),
          0, e.event_date, e.event_datetime,
          NULL, 0, 0, 0,
          LEFT(CONVERT(varchar(50),e.source_record_id),50),
          LEFT(CONVERT(varchar(50),e.source_value),50), NULL, NULL, NULL
        FROM {x} x
        JOIN events e
          ON e.source_domain=x.source_domain AND e.source_record_id=x.source_record_id
        WHERE x.target_domain='Specimen'
        """
    else:
        raise ValueError(domain)
    connection.exec_driver_sql(sql)


def materialize_condition_cross_domain_events(config: EtlConfig) -> dict[str, object]:
    """Materialize canonical DIAGNOSIS/CONDITION routes outside Condition.

    Only canonical core-event routes are materialized. Non-event Standard domains
    and Maps-to-value semantics remain in the canonical routing audit/ledger and
    are not turned into independent clinical event rows.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "condition_cross_domain_materialize.json"

    engine = make_engine(config)
    try:
        with engine.begin() as con:
            _require_tables(con, source_schema, target_schema)
            route_counts = _route_counts(con, target_schema)
            route_total = sum(route_counts.values())

            invalid_targets = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c ON c.concept_id=r.target_concept_id
                WHERE r.is_core_event_route=1
                  AND r.target_domain IN ('Observation','Procedure','Measurement','Drug','Device','Specimen')
                  AND (
                       r.target_concept_id=0
                    OR c.concept_id IS NULL
                    OR c.standard_concept<>'S'
                    OR c.invalid_reason IS NOT NULL
                    OR c.domain_id<>r.target_domain
                  )
                """,
            )
            if invalid_targets:
                raise RuntimeError(
                    f"Canonical Condition cross-domain routes contain {invalid_targets:,} invalid targets"
                )

            cte = _events_cte(source_schema, target_schema)
            resolved_routes = _scalar(
                con,
                cte
                + f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN events e
                  ON e.source_domain=r.source_domain AND e.source_record_id=r.source_record_id
                WHERE r.is_core_event_route=1
                  AND r.target_domain IN ('Observation','Procedure','Measurement','Drug','Device','Specimen')
                """,
            )
            if resolved_routes != route_total:
                raise RuntimeError(
                    "Condition cross-domain route-to-source reconciliation failed: "
                    f"routes={route_total:,}, resolved={resolved_routes:,}"
                )

            target_before = {
                domain: _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{TARGETS[domain][0]}]",
                )
                for domain in DOMAINS
            }

            if table_exists(con, target_schema, XWALK_TABLE):
                status = "already_matched"
                xwalk_total = _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]"
                )
                if xwalk_total != route_total:
                    raise RuntimeError(
                        f"Existing {XWALK_TABLE} has {xwalk_total:,} rows; expected {route_total:,}"
                    )
            else:
                for domain in DOMAINS:
                    accounted = _base_lineage_count(con, target_schema, domain)
                    if target_before[domain] != accounted:
                        raise RuntimeError(
                            f"{domain} target is not pristine for Condition cross-domain append: "
                            f"target={target_before[domain]:,}, base_lineage={accounted:,}"
                        )

                con.exec_driver_sql(
                    f"""
                    CREATE TABLE [{target_schema}].[{XWALK_TABLE}] (
                      route_id bigint NOT NULL,
                      target_domain varchar(32) NOT NULL,
                      target_row_id bigint NOT NULL,
                      source_domain varchar(16) NOT NULL,
                      source_record_id nvarchar(255) NOT NULL,
                      source_concept_id bigint NOT NULL,
                      target_concept_id bigint NOT NULL,
                      route_status varchar(64) NOT NULL,
                      CONSTRAINT PK_{XWALK_TABLE} PRIMARY KEY (route_id),
                      CONSTRAINT UQ_{XWALK_TABLE}_target UNIQUE (target_domain,target_row_id)
                    )
                    """
                )

                for domain in DOMAINS:
                    table, id_col, _ = TARGETS[domain]
                    base_id = _scalar(
                        con,
                        f"SELECT COALESCE(MAX([{id_col}]),0) FROM [{target_schema}].[{table}]",
                    )
                    _insert_xwalk_domain(con, target_schema, domain, base_id)
                    _insert_domain_rows(con, source_schema, target_schema, domain)
                status = "matched"

            target_after: dict[str, int] = {}
            lineage_by_domain: dict[str, int] = {}
            mismatch_by_domain: dict[str, int] = {}
            for domain in DOMAINS:
                table, id_col, concept_col = TARGETS[domain]
                lineage = _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}] "
                    "WHERE target_domain=:domain".replace(":domain", f"'{domain}'"),
                )
                lineage_by_domain[domain] = lineage
                if lineage != route_counts[domain]:
                    raise RuntimeError(
                        f"{domain} cross-domain lineage mismatch: {lineage:,} != {route_counts[domain]:,}"
                    )
                target_after[domain] = _scalar(
                    con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]"
                )
                mismatch = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{XWALK_TABLE}] x
                    LEFT JOIN [{target_schema}].[{ROUTE_TABLE}] r ON r.route_id=x.route_id
                    LEFT JOIN [{target_schema}].[{table}] t ON t.[{id_col}]=x.target_row_id
                    WHERE x.target_domain='{domain}'
                      AND (
                           r.route_id IS NULL
                        OR r.target_domain<>'{domain}'
                        OR r.is_core_event_route<>1
                        OR t.[{id_col}] IS NULL
                        OR t.[{concept_col}]<>x.target_concept_id
                        OR x.target_concept_id<>r.target_concept_id
                      )
                    """,
                )
                mismatch_by_domain[domain] = mismatch
                if mismatch:
                    raise RuntimeError(
                        f"{domain} Condition cross-domain row-level mismatches: {mismatch:,}"
                    )

                expected_after = target_before[domain] if status == "already_matched" else (
                    target_before[domain] + route_counts[domain]
                )
                if status == "matched" and target_after[domain] != expected_after:
                    raise RuntimeError(
                        f"{domain} append count mismatch: {target_after[domain]:,} != {expected_after:,}"
                    )

        payload = {
            "stage": "condition_cross_domain_materialize",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "route_table": f"{target_schema}.{ROUTE_TABLE}",
            "lineage_table": f"{target_schema}.{XWALK_TABLE}",
            "route_rows": route_counts,
            "route_total": route_total,
            "resolved_route_rows": resolved_routes,
            "invalid_target_rows": invalid_targets,
            "target_rows_before": target_before,
            "target_rows_after": target_after,
            "lineage_rows_by_domain": lineage_by_domain,
            "row_level_mismatches": mismatch_by_domain,
            "policy": {
                "event_domains": list(DOMAINS),
                "one_to_many": "materialize every canonical Standard event-domain route",
                "type_concepts": "0 unless exact source-established OMOP provenance semantics exist",
                "dates": (
                    "Use the qualifying source event date. For Drug routes, start and end are both "
                    "the source event date because the source carries no exposure duration semantics."
                ),
                "maps_to_value": "not materialized as an independent event",
                "non_event_domains": "retained in canonical lineage/audit only",
            },
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
