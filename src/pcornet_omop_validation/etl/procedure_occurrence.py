from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_procedure_event_route"
XWALK_TABLE = "etl_procedure_occurrence_xwalk"


@dataclass(frozen=True)
class ProcedureOccurrenceTransformResult:
    source_rows: int
    eligible_source_events: int
    excluded_missing_px_date: int
    procedure_route_rows: int
    distinct_source_procedures: int
    one_to_many_extra_rows: int
    unresolved_rows: int
    unlinked_person_rows: int
    visit_linked_rows: int
    visit_unlinked_rows: int
    target_rows: int
    concept_zero_rows: int
    source_concept_zero_rows: int
    lineage_rows: int
    status: str
    audit_path: Path


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(text(sql)).scalar_one())


def _require_tables(connection, source_schema: str, target_schema: str) -> None:
    required = (
        (source_schema, "PCORnet_PROCEDURES"),
        (target_schema, ROUTE_TABLE),
        (target_schema, "etl_visit_occurrence_xwalk"),
        (target_schema, "person"),
        (target_schema, "procedure_occurrence"),
        (target_schema, "concept"),
    )
    for schema, table in required:
        if not table_exists(connection, schema, table):
            raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")


def transform_procedure_occurrence(
    config: EtlConfig,
) -> ProcedureOccurrenceTransformResult:
    policies = config.raw.get("policies", {}) or {}

    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "Validated procedure ETL requires "
            "policies.missing_required_date=exclude"
        )
    if policies.get("unmapped_standard_concept") != "concept_zero":
        raise RuntimeError(
            "Validated procedure ETL requires "
            "policies.unmapped_standard_concept=concept_zero"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "procedure_occurrence_transform.json"

    engine = make_engine(config)

    try:
        with engine.connect() as connection:
            _require_tables(connection, source_schema, target_schema)

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) "
                f"FROM [{source_schema}].[PCORnet_PROCEDURES]",
            )

            excluded_missing_px_date = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_PROCEDURES]
                WHERE PX_DATE IS NULL
                """,
            )

            eligible_source_events = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(DISTINCT source_procedure_id)
                FROM [{target_schema}].[{ROUTE_TABLE}]
                """,
            )

            procedure_route_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE target_domain = 'Procedure'
                """,
            )

            distinct_source_procedures = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(DISTINCT source_procedure_id)
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE target_domain = 'Procedure'
                """,
            )

            one_to_many_extra_rows = (
                procedure_route_rows - distinct_source_procedures
            )

            unresolved_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE target_domain = 'Procedure'
                  AND target_concept_id = 0
                """,
            )

            invalid_target_concepts = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_domain = 'Procedure'
                  AND r.target_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.standard_concept <> 'S'
                    OR c.domain_id <> 'Procedure'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_target_concepts:
                raise RuntimeError(
                    "Procedure route ledger contains "
                    f"{invalid_target_concepts:,} nonzero target concept(s) "
                    "that are not active Standard Procedure concepts"
                )

            unlinked_person_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[person] p
                  ON r.patid = p.person_source_value
                WHERE r.target_domain = 'Procedure'
                  AND p.person_id IS NULL
                """,
            )
            if unlinked_person_rows:
                raise RuntimeError(
                    f"{unlinked_person_rows:,} Procedure-domain route rows "
                    "cannot be linked to person"
                )

            visit_linked_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                  ON r.encounterid = v.encounterid
                WHERE r.target_domain = 'Procedure'
                """,
            )
            visit_unlinked_rows = procedure_route_rows - visit_linked_rows

            existing = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[procedure_occurrence]
                """,
            )

            if existing:
                if existing != procedure_route_rows:
                    raise RuntimeError(
                        f"Target [{target_schema}].[procedure_occurrence] "
                        f"already contains {existing:,} rows, but "
                        f"{procedure_route_rows:,} Procedure-domain route rows "
                        "are expected. Refusing to append or overwrite."
                    )
                status = "already_loaded_matched"
            else:
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO [{target_schema}].[procedure_occurrence] (
                        procedure_occurrence_id,
                        person_id,
                        procedure_concept_id,
                        procedure_date,
                        procedure_datetime,
                        procedure_end_date,
                        procedure_end_datetime,
                        procedure_type_concept_id,
                        modifier_concept_id,
                        quantity,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        procedure_source_value,
                        procedure_source_concept_id,
                        modifier_source_value
                    )
                    SELECT
                        r.route_id,
                        p.person_id,
                        r.target_concept_id,
                        r.px_date,
                        CAST(r.px_date AS datetime2(7)),
                        NULL,
                        NULL,
                        0,
                        0,
                        NULL,
                        NULL,
                        v.visit_occurrence_id,
                        NULL,
                        r.px,
                        r.source_concept_id,
                        NULL
                    FROM [{target_schema}].[{ROUTE_TABLE}] r
                    JOIN [{target_schema}].[person] p
                      ON r.patid = p.person_source_value
                    LEFT JOIN [{target_schema}].[etl_visit_occurrence_xwalk] v
                      ON r.encounterid = v.encounterid
                    WHERE r.target_domain = 'Procedure'
                    """
                )
                connection.commit()
                status = "matched"

            target_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[procedure_occurrence]
                """,
            )
            if target_rows != procedure_route_rows:
                raise RuntimeError(
                    "Procedure reconciliation failed: "
                    f"route_rows={procedure_route_rows:,}, "
                    f"target_rows={target_rows:,}"
                )

            concept_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[procedure_occurrence]
                WHERE procedure_concept_id = 0
                """,
            )

            source_concept_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[procedure_occurrence]
                WHERE procedure_source_concept_id = 0
                """,
            )

            if table_exists(connection, target_schema, XWALK_TABLE):
                lineage_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{XWALK_TABLE}]
                    """,
                )
                if lineage_rows != procedure_route_rows:
                    raise RuntimeError(
                        f"Existing procedure lineage table contains "
                        f"{lineage_rows:,} rows; expected "
                        f"{procedure_route_rows:,}"
                    )
            else:
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE [{target_schema}].[{XWALK_TABLE}] (
                        procedure_occurrence_id bigint NOT NULL,
                        route_id bigint NOT NULL,
                        source_procedure_id nvarchar(255) NOT NULL,
                        target_ordinal int NOT NULL,
                        source_concept_id bigint NOT NULL,
                        target_concept_id bigint NOT NULL,
                        route_status varchar(64) NOT NULL,
                        CONSTRAINT PK_{XWALK_TABLE}
                            PRIMARY KEY (procedure_occurrence_id),
                        CONSTRAINT UQ_{XWALK_TABLE}_route
                            UNIQUE (route_id),
                        CONSTRAINT UQ_{XWALK_TABLE}_source_ordinal
                            UNIQUE (source_procedure_id, target_ordinal)
                    )
                    """
                )
                connection.exec_driver_sql(
                    f"""
                    INSERT INTO [{target_schema}].[{XWALK_TABLE}] (
                        procedure_occurrence_id,
                        route_id,
                        source_procedure_id,
                        target_ordinal,
                        source_concept_id,
                        target_concept_id,
                        route_status
                    )
                    SELECT
                        route_id,
                        route_id,
                        source_procedure_id,
                        target_ordinal,
                        source_concept_id,
                        target_concept_id,
                        route_status
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    WHERE target_domain = 'Procedure'
                    """
                )
                connection.commit()

                lineage_rows = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{XWALK_TABLE}]
                    """,
                )

            if lineage_rows != target_rows:
                raise RuntimeError(
                    "Procedure lineage reconciliation failed: "
                    f"lineage={lineage_rows:,}, target={target_rows:,}"
                )

    finally:
        engine.dispose()

    payload = asdict(
        ProcedureOccurrenceTransformResult(
            source_rows=source_rows,
            eligible_source_events=eligible_source_events,
            excluded_missing_px_date=excluded_missing_px_date,
            procedure_route_rows=procedure_route_rows,
            distinct_source_procedures=distinct_source_procedures,
            one_to_many_extra_rows=one_to_many_extra_rows,
            unresolved_rows=unresolved_rows,
            unlinked_person_rows=unlinked_person_rows,
            visit_linked_rows=visit_linked_rows,
            visit_unlinked_rows=visit_unlinked_rows,
            target_rows=target_rows,
            concept_zero_rows=concept_zero_rows,
            source_concept_zero_rows=source_concept_zero_rows,
            lineage_rows=lineage_rows,
            status=status,
            audit_path=audit_path,
        )
    )
    payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["stage"] = "procedure_occurrence"
    payload["source_schema"] = source_schema
    payload["target_schema"] = target_schema
    payload["mapping_strategy"] = {
        "source": "etl_procedure_event_route",
        "domain": "Procedure only",
        "one_to_many": (
            "Preserve every distinct active Standard Procedure target; "
            "do not select TOP(1)"
        ),
        "unresolved": (
            "Retain Procedure-domain unresolved routes with "
            "procedure_concept_id=0"
        ),
        "procedure_type_concept_id": (
            "0 because no validated OMOP type mapping is assigned from "
            "PCORnet PX_SOURCE at this stage"
        ),
        "procedure_datetime": (
            "PX_DATE at midnight because PROCEDURES has no source time field"
        ),
        "procedure_end": (
            "NULL because no procedure end date/time exists in the source"
        ),
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    return ProcedureOccurrenceTransformResult(
        source_rows=source_rows,
        eligible_source_events=eligible_source_events,
        excluded_missing_px_date=excluded_missing_px_date,
        procedure_route_rows=procedure_route_rows,
        distinct_source_procedures=distinct_source_procedures,
        one_to_many_extra_rows=one_to_many_extra_rows,
        unresolved_rows=unresolved_rows,
        unlinked_person_rows=unlinked_person_rows,
        visit_linked_rows=visit_linked_rows,
        visit_unlinked_rows=visit_unlinked_rows,
        target_rows=target_rows,
        concept_zero_rows=concept_zero_rows,
        source_concept_zero_rows=source_concept_zero_rows,
        lineage_rows=lineage_rows,
        status=status,
        audit_path=audit_path,
    )
