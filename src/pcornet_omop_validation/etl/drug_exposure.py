from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_drug_exposure_xwalk"
ROUTE_TABLE = "etl_drug_event_route"

# These are stable OMOP provenance concepts.  They are validated against the
# loaded vocabulary before use rather than treated as dataset-specific facts.
TYPE_CONCEPTS = {
    "PRESCRIBING": (32838, "Type Concept", "Type Concept", "OMOP4976911"),
    "DISPENSING": (32825, "Type Concept", "Type Concept", "OMOP4976898"),
    "MED_ADMIN": (32818, "Type Concept", "Type Concept", "OMOP4976891"),
    "IMMUNIZATION": (32818, "Type Concept", "Type Concept", "OMOP4976891"),
    "PROCEDURES": (38000179, "Type Concept", "Drug Type", "OMOP4822243"),
}

FAMILIES = tuple(TYPE_CONCEPTS)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def _datetime_sql(date_expr: str, time_expr: str | None = None) -> str:
    if time_expr is None:
        return f"CAST(CAST({date_expr} AS date) AS datetime2(7))"
    seconds = f"TRY_CONVERT(float, {time_expr})"
    return f"""
    CASE
      WHEN {date_expr} IS NULL THEN NULL
      WHEN {seconds} IS NULL OR {seconds} < 0 OR {seconds} >= 86400
        THEN CAST(CAST({date_expr} AS date) AS datetime2(7))
      ELSE DATEADD(
        MILLISECOND,
        CAST(ROUND({seconds} * 1000.0, 0) AS bigint),
        CAST(CAST({date_expr} AS date) AS datetime2(7))
      )
    END
    """.strip()


def _require_tables(con, source_schema: str, target_schema: str) -> None:
    source_tables = (
        "PCORnet_PRESCRIBING",
        "PCORnet_DISPENSING",
        "PCORnet_MED_ADMIN",
        "PCORnet_IMMUNIZATION",
        "PCORnet_PROCEDURES",
    )
    target_tables = (
        "drug_exposure",
        ROUTE_TABLE,
        "person",
        "etl_visit_occurrence_xwalk",
        "concept",
    )
    for table in source_tables:
        if not table_exists(con, source_schema, table):
            raise RuntimeError(
                f"Required table [{source_schema}].[{table}] does not exist"
            )
    for table in target_tables:
        if not table_exists(con, target_schema, table):
            raise RuntimeError(
                f"Required table [{target_schema}].[{table}] does not exist"
            )


def _validate_type_concepts(con, target_schema: str) -> dict[str, dict[str, object]]:
    observed: dict[str, dict[str, object]] = {}
    for family, (concept_id, domain_id, vocabulary_id, concept_code) in TYPE_CONCEPTS.items():
        row = con.execute(
            text(
                f"""
                SELECT concept_id, domain_id, vocabulary_id, concept_code,
                       standard_concept, invalid_reason
                FROM [{target_schema}].[concept]
                WHERE concept_id = :concept_id
                """
            ),
            {"concept_id": concept_id},
        ).mappings().one_or_none()
        if row is None:
            raise RuntimeError(
                f"Required Drug Type concept {concept_id} for {family} is absent"
            )
        if (
            row["domain_id"] != domain_id
            or row["vocabulary_id"] != vocabulary_id
            or row["concept_code"] != concept_code
            or row["invalid_reason"] is not None
        ):
            raise RuntimeError(
                f"Drug Type concept {concept_id} no longer matches validated "
                f"semantics for {family}: {dict(row)}"
            )
        observed[family] = dict(row)
    return observed


def _family_source_counts(con, source_schema: str) -> dict[str, int]:
    queries = {
        "PRESCRIBING": f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_PRESCRIBING]",
        "DISPENSING": f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_DISPENSING]",
        "MED_ADMIN": f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_MED_ADMIN]",
        "IMMUNIZATION": f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_IMMUNIZATION]",
        "PROCEDURES": f"""
            SELECT COUNT_BIG(DISTINCT r.source_record_id)
            FROM [{source_schema}].[PCORnet_PROCEDURES] p
            JOIN [{source_schema}].[PCORnet_PROCEDURES] p2
              ON 1 = 0
            RIGHT JOIN (SELECT CAST(NULL AS nvarchar(255)) AS source_record_id) r
              ON 1 = 0
        """,
    }
    # PROCEDURES source events for Drug Exposure are defined by the canonical
    # Drug route ledger, not by all rows in PCORnet_PROCEDURES.  It is populated
    # below from that ledger and therefore not evaluated with the placeholder.
    result = {
        family: _scalar(con, sql)
        for family, sql in queries.items()
        if family != "PROCEDURES"
    }
    return result


def _source_diagnostics(
    con,
    source_schema: str,
    target_schema: str,
) -> dict[str, dict[str, int]]:
    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    specs = {
        "PRESCRIBING": (
            "PCORnet_PRESCRIBING",
            "PRESCRIBINGID",
            "PATID",
            "COALESCE(RX_START_DATE, RX_ORDER_DATE)",
        ),
        "DISPENSING": (
            "PCORnet_DISPENSING",
            "DISPENSINGID",
            "PATID",
            "DISPENSE_DATE",
        ),
        "MED_ADMIN": (
            "PCORnet_MED_ADMIN",
            "MEDADMINID",
            "PATID",
            "MEDADMIN_START_DATE",
        ),
        "IMMUNIZATION": (
            "PCORnet_IMMUNIZATION",
            "IMMUNIZATIONID",
            "PATID",
            "VX_ADMIN_DATE",
        ),
    }

    out: dict[str, dict[str, int]] = {}
    for family, (table, id_col, patid_col, start_expr) in specs.items():
        source_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {s(table)}")
        missing_id = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*) FROM {s(table)}
            WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))), '') IS NULL
            """,
        )
        duplicate_ids = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*) FROM (
                SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))) AS source_id
                FROM {s(table)}
                WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))), '') IS NOT NULL
                GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col})))
                HAVING COUNT_BIG(*) > 1
            ) q
            """,
        )
        missing_start = _scalar(
            con,
            f"SELECT COUNT_BIG(*) FROM {s(table)} WHERE {start_expr} IS NULL",
        )
        unlinked_person = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM {s(table)} src
            LEFT JOIN {t('person')} p
              ON p.person_source_value =
                 LTRIM(RTRIM(CONVERT(nvarchar(255), src.{patid_col})))
            WHERE p.person_id IS NULL
            """,
        )
        out[family] = {
            "source_rows": source_rows,
            "missing_source_id": missing_id,
            "duplicate_source_id_groups": duplicate_ids,
            "missing_required_start_date": missing_start,
            "unlinked_person_source_rows": unlinked_person,
        }
        if missing_id or duplicate_ids:
            raise RuntimeError(
                f"{family} source identifiers are not unique and complete: "
                f"missing={missing_id:,}, duplicate_groups={duplicate_ids:,}"
            )

    # PROCEDURES Drug source events are the subset selected by the canonical
    # procedure route ledger.  Validate required date/person linkage there.
    procedure_route_rows = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)}
        WHERE source_domain = 'PROCEDURES'
        """,
    )
    procedure_distinct = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(DISTINCT source_record_id) FROM {t(ROUTE_TABLE)}
        WHERE source_domain = 'PROCEDURES'
        """,
    )
    procedure_missing_date_routes = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM {t(ROUTE_TABLE)} r
        LEFT JOIN {s('PCORnet_PROCEDURES')} p
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PROCEDURESID))) = r.source_record_id
        WHERE r.source_domain = 'PROCEDURES'
          AND p.PX_DATE IS NULL
        """,
    )
    procedure_unlinked_person_routes = _scalar(
        con,
        f"""
        SELECT COUNT_BIG(*)
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_PROCEDURES')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.PROCEDURESID))) = r.source_record_id
        LEFT JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'PROCEDURES'
          AND p.person_id IS NULL
        """,
    )
    out["PROCEDURES"] = {
        "source_rows": procedure_distinct,
        "route_rows": procedure_route_rows,
        "missing_source_id": 0,
        "duplicate_source_id_groups": 0,
        "missing_required_start_date": procedure_missing_date_routes,
        "unlinked_person_source_rows": procedure_unlinked_person_routes,
    }
    return out


def _eligible_route_cte(source_schema: str, target_schema: str) -> str:
    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"
    return f"""
    WITH eligible_route AS (
        SELECT r.route_id, r.source_domain, r.source_record_id,
               r.mapping_basis,
               'RX_START_DATE_OR_RX_ORDER_DATE' AS date_basis
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_PRESCRIBING')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.PRESCRIBINGID))) = r.source_record_id
        JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'PRESCRIBING'
          AND COALESCE(src.RX_START_DATE, src.RX_ORDER_DATE) IS NOT NULL

        UNION ALL

        SELECT r.route_id, r.source_domain, r.source_record_id,
               r.mapping_basis, 'DISPENSE_DATE'
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_DISPENSING')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.DISPENSINGID))) = r.source_record_id
        JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'DISPENSING'
          AND src.DISPENSE_DATE IS NOT NULL

        UNION ALL

        SELECT r.route_id, r.source_domain, r.source_record_id,
               r.mapping_basis, 'MEDADMIN_START_DATE'
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_MED_ADMIN')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.MEDADMINID))) = r.source_record_id
        JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'MED_ADMIN'
          AND src.MEDADMIN_START_DATE IS NOT NULL

        UNION ALL

        SELECT r.route_id, r.source_domain, r.source_record_id,
               r.mapping_basis, 'VX_ADMIN_DATE'
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_IMMUNIZATION')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.IMMUNIZATIONID))) = r.source_record_id
        JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'IMMUNIZATION'
          AND src.VX_ADMIN_DATE IS NOT NULL

        UNION ALL

        SELECT r.route_id, r.source_domain, r.source_record_id,
               r.mapping_basis, 'PX_DATE'
        FROM {t(ROUTE_TABLE)} r
        JOIN {s('PCORnet_PROCEDURES')} src
          ON LTRIM(RTRIM(CONVERT(nvarchar(255), src.PROCEDURESID))) = r.source_record_id
        JOIN {t('person')} p
          ON p.person_source_value =
             LTRIM(RTRIM(CONVERT(nvarchar(255), src.PATID)))
        WHERE r.source_domain = 'PROCEDURES'
          AND src.PX_DATE IS NOT NULL
    )
    """


def transform_drug_exposure(config: EtlConfig) -> dict[str, object]:
    """Materialize Drug Exposure from the canonical Drug route ledger.

    Primary transformation rules are source-semantic and vocabulary driven.
    Dataset-specific row totals are never used as acceptance criteria.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path: Path = config.audit_dir / "drug_exposure_transform.json"

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"
    engine = make_engine(config)

    try:
        with engine.begin() as con:
            _require_tables(con, source_schema, target_schema)
            type_concepts = _validate_type_concepts(con, target_schema)
            diagnostics = _source_diagnostics(con, source_schema, target_schema)

            route_total = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)}")
            invalid_route_targets = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(ROUTE_TABLE)} r
                LEFT JOIN {t('concept')} c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.domain_id <> 'Drug'
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_route_targets:
                raise RuntimeError(
                    f"Drug route ledger contains {invalid_route_targets:,} "
                    "invalid nonzero Drug targets"
                )

            eligible_cte = _eligible_route_cte(source_schema, target_schema)
            eligible_rows = _scalar(
                con,
                eligible_cte + " SELECT COUNT_BIG(*) FROM eligible_route",
            )
            eligible_distinct_routes = _scalar(
                con,
                eligible_cte
                + " SELECT COUNT_BIG(DISTINCT route_id) FROM eligible_route",
            )
            if eligible_rows != eligible_distinct_routes:
                raise RuntimeError(
                    "Eligible Drug routes are not unique by route_id: "
                    f"rows={eligible_rows:,}, distinct={eligible_distinct_routes:,}"
                )

            family_route_counts = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    text(
                        eligible_cte
                        + """
                        SELECT source_domain, COUNT_BIG(*)
                        FROM eligible_route
                        GROUP BY source_domain
                        """
                    )
                ).all()
            }
            eligible_concept_zero = _scalar(
                con,
                eligible_cte
                + f"""
                SELECT COUNT_BIG(*)
                FROM eligible_route e
                JOIN {t(ROUTE_TABLE)} r ON r.route_id = e.route_id
                WHERE r.target_concept_id = 0
                """,
            )

            xwalk_exists = table_exists(con, target_schema, XWALK_TABLE)
            target_rows_before = _scalar(
                con, f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')}"
            )
            xwalk_rows_before = (
                _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}")
                if xwalk_exists
                else 0
            )

            if target_rows_before:
                if not xwalk_exists:
                    raise RuntimeError(
                        "drug_exposure is populated but Drug lineage is absent"
                    )
                if target_rows_before != eligible_rows or xwalk_rows_before != eligible_rows:
                    raise RuntimeError(
                        "Existing Drug Exposure does not match source-derived eligible routes: "
                        f"eligible={eligible_rows:,}, target={target_rows_before:,}, "
                        f"lineage={xwalk_rows_before:,}"
                    )
                orphan_lineage = _scalar(
                    con,
                    eligible_cte
                    + f"""
                    SELECT COUNT_BIG(*)
                    FROM {t(XWALK_TABLE)} x
                    LEFT JOIN eligible_route e ON e.route_id = x.route_id
                    WHERE e.route_id IS NULL
                    """,
                )
                missing_lineage = _scalar(
                    con,
                    eligible_cte
                    + f"""
                    SELECT COUNT_BIG(*)
                    FROM eligible_route e
                    LEFT JOIN {t(XWALK_TABLE)} x ON x.route_id = e.route_id
                    WHERE x.route_id IS NULL
                    """,
                )
                if orphan_lineage or missing_lineage:
                    raise RuntimeError(
                        "Existing Drug lineage disagrees with eligible route set: "
                        f"orphan={orphan_lineage:,}, missing={missing_lineage:,}"
                    )
                status = "already_matched"
            else:
                if xwalk_exists and xwalk_rows_before:
                    raise RuntimeError(
                        "Drug lineage is populated while drug_exposure is empty"
                    )
                if not xwalk_exists:
                    con.execute(
                        text(
                            f"""
                            CREATE TABLE {t(XWALK_TABLE)} (
                                drug_exposure_id int NOT NULL PRIMARY KEY,
                                route_id bigint NOT NULL UNIQUE,
                                source_domain varchar(32) NOT NULL,
                                source_record_id nvarchar(255) NOT NULL,
                                mapping_basis varchar(32) NOT NULL,
                                date_basis varchar(64) NOT NULL
                            );
                            CREATE INDEX IX_{XWALK_TABLE}_source
                            ON {t(XWALK_TABLE)} (source_domain, source_record_id);
                            """
                        )
                    )

                if eligible_rows > 2_147_483_647:
                    raise RuntimeError(
                        "Eligible Drug Exposure rows exceed SQL Server int key capacity"
                    )

                con.execute(
                    text(
                        eligible_cte
                        + f"""
                        INSERT INTO {t(XWALK_TABLE)} (
                            drug_exposure_id, route_id, source_domain,
                            source_record_id, mapping_basis, date_basis
                        )
                        SELECT
                            CONVERT(int, ROW_NUMBER() OVER (ORDER BY route_id)),
                            route_id,
                            source_domain,
                            source_record_id,
                            mapping_basis,
                            date_basis
                        FROM eligible_route
                        """
                    )
                )

                rx_start = "COALESCE(CAST(p.RX_START_DATE AS date), CAST(p.RX_ORDER_DATE AS date))"
                rx_start_dt = f"""
                    CASE WHEN p.RX_START_DATE IS NOT NULL
                         THEN {_datetime_sql('p.RX_START_DATE')}
                         ELSE {_datetime_sql('p.RX_ORDER_DATE', 'p.RX_ORDER_TIME')}
                    END
                """
                rx_end = f"""
                    CASE
                      WHEN p.RX_END_DATE IS NOT NULL
                       AND CAST(p.RX_END_DATE AS date) >= {rx_start}
                        THEN CAST(p.RX_END_DATE AS date)
                      WHEN p.RX_END_DATE IS NOT NULL
                       AND CAST(p.RX_END_DATE AS date) < {rx_start}
                        THEN {rx_start}
                      WHEN TRY_CONVERT(int, p.RX_DAYS_SUPPLY) > 0
                        THEN DATEADD(DAY, TRY_CONVERT(int, p.RX_DAYS_SUPPLY) - 1, {rx_start})
                      ELSE {rx_start}
                    END
                """
                dispense_end = """
                    CASE WHEN TRY_CONVERT(int, d.DISPENSE_SUP) > 0
                         THEN DATEADD(DAY, TRY_CONVERT(int, d.DISPENSE_SUP) - 1,
                                      CAST(d.DISPENSE_DATE AS date))
                         ELSE CAST(d.DISPENSE_DATE AS date)
                    END
                """
                med_start = "CAST(m.MEDADMIN_START_DATE AS date)"
                med_end = f"""
                    CASE
                      WHEN m.MEDADMIN_STOP_DATE IS NULL THEN {med_start}
                      WHEN CAST(m.MEDADMIN_STOP_DATE AS date) < {med_start} THEN {med_start}
                      ELSE CAST(m.MEDADMIN_STOP_DATE AS date)
                    END
                """
                med_start_dt = _datetime_sql("m.MEDADMIN_START_DATE", "m.MEDADMIN_START_TIME")
                med_end_dt = f"""
                    CASE
                      WHEN m.MEDADMIN_STOP_DATE IS NULL
                        THEN {med_start_dt}
                      WHEN CAST(m.MEDADMIN_STOP_DATE AS date) < {med_start}
                        THEN {med_start_dt}
                      ELSE {_datetime_sql('m.MEDADMIN_STOP_DATE', 'm.MEDADMIN_STOP_TIME')}
                    END
                """

                con.execute(
                    text(
                        f"""
                        WITH normalized AS (
                            SELECT
                                x.drug_exposure_id,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), p.PATID))) AS patid,
                                r.target_concept_id AS drug_concept_id,
                                {rx_start} AS start_date,
                                {rx_start_dt} AS start_datetime,
                                {rx_end} AS end_date,
                                CAST({rx_end} AS datetime2(7)) AS end_datetime,
                                CAST(p.RX_END_DATE AS date) AS verbatim_end_date,
                                {TYPE_CONCEPTS['PRESCRIBING'][0]} AS type_concept_id,
                                TRY_CONVERT(int, p.RX_REFILLS) AS refills,
                                TRY_CONVERT(float, p.RX_QUANTITY) AS quantity,
                                TRY_CONVERT(int, p.RX_DAYS_SUPPLY) AS days_supply,
                                LEFT(COALESCE(CONVERT(varchar(max), p.RAW_RX_FREQUENCY),
                                              CONVERT(varchar(max), p.RX_FREQUENCY)), 8000) AS sig,
                                LEFT(COALESCE(CONVERT(varchar(50), r.source_code),
                                              CONVERT(varchar(50), p.RAW_RX_NDC),
                                              CONVERT(varchar(50), p.RXNORM_CUI)), 50) AS source_value,
                                r.source_concept_id,
                                LEFT(COALESCE(CONVERT(varchar(50), p.RAW_RX_ROUTE),
                                              CONVERT(varchar(50), p.RX_ROUTE)), 50) AS route_source_value,
                                LEFT(COALESCE(CONVERT(varchar(50), p.RAW_RX_DOSE_ORDERED_UNIT),
                                              CONVERT(varchar(50), p.RX_DOSE_ORDERED_UNIT)), 50) AS dose_unit_source_value,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), p.ENCOUNTERID))) AS encounterid
                            FROM {t(ROUTE_TABLE)} r
                            JOIN {t(XWALK_TABLE)} x ON x.route_id = r.route_id
                            JOIN {s('PCORnet_PRESCRIBING')} p
                              ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PRESCRIBINGID))) = r.source_record_id
                            WHERE r.source_domain = 'PRESCRIBING'

                            UNION ALL

                            SELECT
                                x.drug_exposure_id,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))),
                                r.target_concept_id,
                                CAST(d.DISPENSE_DATE AS date),
                                {_datetime_sql('d.DISPENSE_DATE')},
                                {dispense_end},
                                CAST({dispense_end} AS datetime2(7)),
                                NULL,
                                {TYPE_CONCEPTS['DISPENSING'][0]},
                                NULL,
                                TRY_CONVERT(float, d.DISPENSE_AMT),
                                TRY_CONVERT(int, d.DISPENSE_SUP),
                                NULL,
                                LEFT(CONVERT(varchar(50), d.NDC), 50),
                                r.source_concept_id,
                                LEFT(COALESCE(CONVERT(varchar(50), d.RAW_DISPENSE_ROUTE),
                                              CONVERT(varchar(50), d.DISPENSE_ROUTE)), 50),
                                LEFT(COALESCE(CONVERT(varchar(50), d.RAW_DISPENSE_DOSE_DISP_UNIT),
                                              CONVERT(varchar(50), d.DISPENSE_DOSE_DISP_UNIT)), 50),
                                NULL
                            FROM {t(ROUTE_TABLE)} r
                            JOIN {t(XWALK_TABLE)} x ON x.route_id = r.route_id
                            JOIN {s('PCORnet_DISPENSING')} d
                              ON LTRIM(RTRIM(CONVERT(nvarchar(255), d.DISPENSINGID))) = r.source_record_id
                            WHERE r.source_domain = 'DISPENSING'

                            UNION ALL

                            SELECT
                                x.drug_exposure_id,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), m.PATID))),
                                r.target_concept_id,
                                {med_start},
                                {med_start_dt},
                                {med_end},
                                {med_end_dt},
                                CAST(m.MEDADMIN_STOP_DATE AS date),
                                {TYPE_CONCEPTS['MED_ADMIN'][0]},
                                NULL,
                                TRY_CONVERT(float, m.MEDADMIN_DOSE_ADMIN),
                                NULL,
                                NULL,
                                LEFT(COALESCE(CONVERT(varchar(50), r.source_code),
                                              CONVERT(varchar(50), m.RAW_MEDADMIN_CODE),
                                              CONVERT(varchar(50), m.MEDADMIN_CODE)), 50),
                                r.source_concept_id,
                                LEFT(COALESCE(CONVERT(varchar(50), m.RAW_MEDADMIN_ROUTE),
                                              CONVERT(varchar(50), m.MEDADMIN_ROUTE)), 50),
                                LEFT(COALESCE(CONVERT(varchar(50), m.RAW_MEDADMIN_DOSE_ADMIN_UNIT),
                                              CONVERT(varchar(50), m.MEDADMIN_DOSE_ADMIN_UNIT)), 50),
                                LTRIM(RTRIM(CONVERT(nvarchar(255), m.ENCOUNTERID)))
                            FROM {t(ROUTE_TABLE)} r
                            JOIN {t(XWALK_TABLE)} x ON x.route_id = r.route_id
                            JOIN {s('PCORnet_MED_ADMIN')} m
                              ON LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMINID))) = r.source_record_id
                            WHERE r.source_domain = 'MED_ADMIN'

                            UNION ALL

                            SELECT
                                x.drug_exposure_id,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), i.PATID))),
                                r.target_concept_id,
                                CAST(i.VX_ADMIN_DATE AS date),
                                {_datetime_sql('i.VX_ADMIN_DATE')},
                                CAST(i.VX_ADMIN_DATE AS date),
                                {_datetime_sql('i.VX_ADMIN_DATE')},
                                CAST(i.VX_ADMIN_DATE AS date),
                                {TYPE_CONCEPTS['IMMUNIZATION'][0]},
                                NULL, NULL, NULL, NULL,
                                LEFT(CONVERT(varchar(50), i.VX_CODE), 50),
                                r.source_concept_id,
                                LEFT(CONVERT(varchar(50), i.VX_ROUTE), 50),
                                LEFT(CONVERT(varchar(50), i.VX_DOSE_UNIT), 50),
                                LTRIM(RTRIM(CONVERT(nvarchar(255), i.ENCOUNTERID)))
                            FROM {t(ROUTE_TABLE)} r
                            JOIN {t(XWALK_TABLE)} x ON x.route_id = r.route_id
                            JOIN {s('PCORnet_IMMUNIZATION')} i
                              ON LTRIM(RTRIM(CONVERT(nvarchar(255), i.IMMUNIZATIONID))) = r.source_record_id
                            WHERE r.source_domain = 'IMMUNIZATION'

                            UNION ALL

                            SELECT
                                x.drug_exposure_id,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), p.PATID))),
                                r.target_concept_id,
                                CAST(p.PX_DATE AS date),
                                {_datetime_sql('p.PX_DATE')},
                                CAST(p.PX_DATE AS date),
                                {_datetime_sql('p.PX_DATE')},
                                CAST(p.PX_DATE AS date),
                                {TYPE_CONCEPTS['PROCEDURES'][0]},
                                NULL, NULL, NULL, NULL,
                                LEFT(CONVERT(varchar(50), p.PX), 50),
                                r.source_concept_id,
                                NULL,
                                NULL,
                                LTRIM(RTRIM(CONVERT(nvarchar(255), p.ENCOUNTERID)))
                            FROM {t(ROUTE_TABLE)} r
                            JOIN {t(XWALK_TABLE)} x ON x.route_id = r.route_id
                            JOIN {s('PCORnet_PROCEDURES')} p
                              ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PROCEDURESID))) = r.source_record_id
                            WHERE r.source_domain = 'PROCEDURES'
                        )
                        INSERT INTO {t('drug_exposure')} (
                            drug_exposure_id, person_id, drug_concept_id,
                            drug_exposure_start_date, drug_exposure_start_datetime,
                            drug_exposure_end_date, drug_exposure_end_datetime,
                            verbatim_end_date, drug_type_concept_id,
                            stop_reason, refills, quantity, days_supply, sig,
                            route_concept_id, lot_number, provider_id,
                            visit_occurrence_id, visit_detail_id,
                            drug_source_value, drug_source_concept_id,
                            route_source_value, dose_unit_source_value
                        )
                        SELECT
                            n.drug_exposure_id,
                            p.person_id,
                            n.drug_concept_id,
                            n.start_date,
                            n.start_datetime,
                            n.end_date,
                            n.end_datetime,
                            n.verbatim_end_date,
                            n.type_concept_id,
                            NULL,
                            n.refills,
                            n.quantity,
                            n.days_supply,
                            n.sig,
                            0,
                            NULL,
                            NULL,
                            vx.visit_occurrence_id,
                            NULL,
                            n.source_value,
                            n.source_concept_id,
                            n.route_source_value,
                            n.dose_unit_source_value
                        FROM normalized n
                        JOIN {t('person')} p ON p.person_source_value = n.patid
                        LEFT JOIN {t('etl_visit_occurrence_xwalk')} vx
                          ON vx.encounterid = n.encounterid
                        """
                    )
                )
                status = "matched"

            target_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')}")
            lineage_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(XWALK_TABLE)}")
            concept_zero = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')} WHERE drug_concept_id = 0",
            )
            null_start = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')} WHERE drug_exposure_start_date IS NULL",
            )
            null_end = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')} WHERE drug_exposure_end_date IS NULL",
            )
            reversed_rows = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*) FROM {t('drug_exposure')}
                WHERE drug_exposure_end_date < drug_exposure_start_date
                """,
            )
            visit_linked = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t('drug_exposure')} WHERE visit_occurrence_id IS NOT NULL",
            )

            if target_rows != eligible_rows or lineage_rows != eligible_rows:
                raise RuntimeError(
                    "Drug Exposure reconciliation failed: "
                    f"eligible={eligible_rows:,}, target={target_rows:,}, "
                    f"lineage={lineage_rows:,}"
                )
            if concept_zero != eligible_concept_zero:
                raise RuntimeError(
                    "Drug concept-zero reconciliation failed: "
                    f"expected={eligible_concept_zero:,}, actual={concept_zero:,}"
                )
            if null_start or null_end or reversed_rows:
                raise RuntimeError(
                    "Drug date invariants failed: "
                    f"null_start={null_start:,}, null_end={null_end:,}, "
                    f"reversed={reversed_rows:,}"
                )

            family_counts = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    text(
                        f"""
                        SELECT x.source_domain, COUNT_BIG(*)
                        FROM {t('drug_exposure')} d
                        JOIN {t(XWALK_TABLE)} x
                          ON x.drug_exposure_id = d.drug_exposure_id
                        GROUP BY x.source_domain
                        """
                    )
                ).all()
            }
            if family_counts != family_route_counts:
                raise RuntimeError(
                    "Drug family reconciliation failed: "
                    f"expected={family_route_counts}, actual={family_counts}"
                )

            prescribing_end_basis = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    text(
                        f"""
                        SELECT
                            CASE
                              WHEN p.RX_END_DATE IS NOT NULL
                               AND CAST(p.RX_END_DATE AS date) >= COALESCE(
                                   CAST(p.RX_START_DATE AS date), CAST(p.RX_ORDER_DATE AS date))
                                THEN 'RX_END_DATE'
                              WHEN p.RX_END_DATE IS NOT NULL
                               AND CAST(p.RX_END_DATE AS date) < COALESCE(
                                   CAST(p.RX_START_DATE AS date), CAST(p.RX_ORDER_DATE AS date))
                                THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                              WHEN TRY_CONVERT(int, p.RX_DAYS_SUPPLY) > 0
                                THEN 'DAYS_SUPPLY_DERIVED'
                              ELSE 'START_DATE_FALLBACK'
                            END AS basis,
                            COUNT_BIG(*)
                        FROM {t(XWALK_TABLE)} x
                        JOIN {s('PCORnet_PRESCRIBING')} p
                          ON LTRIM(RTRIM(CONVERT(nvarchar(255), p.PRESCRIBINGID))) = x.source_record_id
                        WHERE x.source_domain = 'PRESCRIBING'
                        GROUP BY
                            CASE
                              WHEN p.RX_END_DATE IS NOT NULL
                               AND CAST(p.RX_END_DATE AS date) >= COALESCE(
                                   CAST(p.RX_START_DATE AS date), CAST(p.RX_ORDER_DATE AS date))
                                THEN 'RX_END_DATE'
                              WHEN p.RX_END_DATE IS NOT NULL
                               AND CAST(p.RX_END_DATE AS date) < COALESCE(
                                   CAST(p.RX_START_DATE AS date), CAST(p.RX_ORDER_DATE AS date))
                                THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                              WHEN TRY_CONVERT(int, p.RX_DAYS_SUPPLY) > 0
                                THEN 'DAYS_SUPPLY_DERIVED'
                              ELSE 'START_DATE_FALLBACK'
                            END
                        """
                    )
                ).all()
            }

            med_admin_end_basis = {
                str(row[0]): int(row[1])
                for row in con.execute(
                    text(
                        f"""
                        SELECT
                            CASE
                              WHEN m.MEDADMIN_STOP_DATE IS NULL THEN 'START_DATE_FALLBACK'
                              WHEN CAST(m.MEDADMIN_STOP_DATE AS date) < CAST(m.MEDADMIN_START_DATE AS date)
                                THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                              ELSE 'MEDADMIN_STOP_DATE'
                            END,
                            COUNT_BIG(*)
                        FROM {t(XWALK_TABLE)} x
                        JOIN {s('PCORnet_MED_ADMIN')} m
                          ON LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMINID))) = x.source_record_id
                        WHERE x.source_domain = 'MED_ADMIN'
                        GROUP BY
                            CASE
                              WHEN m.MEDADMIN_STOP_DATE IS NULL THEN 'START_DATE_FALLBACK'
                              WHEN CAST(m.MEDADMIN_STOP_DATE AS date) < CAST(m.MEDADMIN_START_DATE AS date)
                                THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                              ELSE 'MEDADMIN_STOP_DATE'
                            END
                        """
                    )
                ).all()
            }

        payload = {
            "stage": "drug_exposure_transform",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "route_rows": route_total,
            "eligible_route_rows": eligible_rows,
            "excluded_route_rows": route_total - eligible_rows,
            "family_route_counts": family_route_counts,
            "family_counts": family_counts,
            "source_diagnostics": diagnostics,
            "target_rows": target_rows,
            "lineage_rows": lineage_rows,
            "concept_zero_rows": concept_zero,
            "visit_linked_rows": visit_linked,
            "null_start_date_rows": null_start,
            "null_end_date_rows": null_end,
            "end_before_start_rows": reversed_rows,
            "prescribing_end_date_basis": prescribing_end_basis,
            "med_admin_end_date_basis": med_admin_end_basis,
            "type_concepts": type_concepts,
            "status": status,
            "policies": {
                "required_start_date": (
                    "Exclude source events without the family-specific required start date; "
                    "quantify exclusions rather than inventing a sentinel date."
                ),
                "person_linkage": (
                    "Exclude source events that cannot be linked to a materialized OMOP person; "
                    "quantify exclusions."
                ),
                "prescribing_end": (
                    "Use valid RX_END_DATE; clamp reversed source intervals to start while "
                    "preserving verbatim_end_date; otherwise derive from positive days supply; "
                    "otherwise use start date."
                ),
                "med_admin_end": (
                    "Use valid MEDADMIN_STOP_DATE; if missing or reversed, use start date while "
                    "preserving the source stop date in verbatim_end_date when present."
                ),
                "mapping": (
                    "Drug concept comes only from the canonical Drug route ledger; unresolved "
                    "routes remain drug_concept_id=0."
                ),
                "route": (
                    "route_concept_id is initialized to 0 and finalized separately from "
                    "standardized PCORnet route semantics."
                ),
            },
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
