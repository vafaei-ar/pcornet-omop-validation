from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_drug_exposure_xwalk"

EXPECTED = {
    "PRESCRIBING": 24_257_671,
    "DISPENSING": 8_370_247,
    "MED_ADMIN": 14_097_424,
    "IMMUNIZATION": 21_132,
    "PROCEDURES": 1_711_406,
}

EXPECTED_TOTAL = 48_457_880
EXPECTED_CONCEPT_ZERO = 17_469_480
EXPECTED_INVALID_SOURCE_INTERVAL_SOURCE_EVENTS = 27_662
EXPECTED_INVALID_SOURCE_INTERVAL_ROUTE_ROWS = 27_842

TYPE_CONCEPTS = {
    "PRESCRIBING": 32838,     # EHR prescription
    "DISPENSING": 32825,      # EHR dispensing record
    "MED_ADMIN": 32818,       # EHR administration record
    "IMMUNIZATION": 32818,    # EHR administration record
    "PROCEDURES": 38000179,   # physician administered drug via procedure
}


def _datetime_sql(date_expr: str, time_expr: str | None = None) -> str:
    if time_expr is None:
        return f"CAST(CAST({date_expr} AS date) AS datetime)"

    return f"""
    CASE
      WHEN {date_expr} IS NULL THEN NULL
      WHEN TRY_CONVERT(float, {time_expr}) IS NULL
        OR TRY_CONVERT(float, {time_expr}) < 0
        OR TRY_CONVERT(float, {time_expr}) >= 86400
        THEN CAST(CAST({date_expr} AS date) AS datetime)
      ELSE DATEADD(
        SECOND,
        CAST(FLOOR(TRY_CONVERT(float, {time_expr})) AS int),
        CAST(CAST({date_expr} AS date) AS datetime)
      )
    END
    """


def transform_drug_exposure(config: EtlConfig) -> dict[str, object]:
    engine = make_engine(config)
    audit_path = config.audit_dir / "drug_exposure_transform.json"

    try:
        with engine.begin() as con:
            required = (
                "drug_exposure",
                "etl_drug_event_route",
                "PCORnet_PRESCRIBING",
                "PCORnet_DISPENSING",
                "PCORnet_MED_ADMIN",
                "PCORnet_IMMUNIZATION",
                "PCORnet_PROCEDURES",
                "person",
                "etl_visit_occurrence_xwalk",
                "concept",
            )

            for table in required:
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(
                        f"Required table dbo.{table} does not exist"
                    )

            current = int(
                con.execute(
                    text("SELECT COUNT_BIG(*) FROM dbo.drug_exposure")
                ).scalar_one()
            )

            if current not in (0, EXPECTED_TOTAL):
                raise RuntimeError(
                    "Unexpected pre-transform drug_exposure row count: "
                    f"{current:,}"
                )

            if current == EXPECTED_TOTAL:
                xwalk = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM dbo.{XWALK_TABLE}
                        """)
                    ).scalar_one()
                ) if table_exists(con, "dbo", XWALK_TABLE) else 0

                if xwalk == EXPECTED_TOTAL:
                    return {
                        "status": "already_matched",
                        "target_rows": current,
                        "lineage_rows": xwalk,
                        "audit_path": str(audit_path),
                    }

                raise RuntimeError(
                    "drug_exposure is populated but lineage is not "
                    "reconciled"
                )

            route_total = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_drug_event_route
                    """)
                ).scalar_one()
            )

            if route_total != EXPECTED_TOTAL:
                raise RuntimeError(
                    f"Drug route count changed: {route_total:,} != "
                    f"{EXPECTED_TOTAL:,}"
                )

            route_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.etl_drug_event_route
                        WHERE target_concept_id = 0
                    """)
                ).scalar_one()
            )

            if route_zero != EXPECTED_CONCEPT_ZERO:
                raise RuntimeError(
                    f"Drug route concept-zero changed: {route_zero:,}"
                )

            for family, expected in EXPECTED.items():
                n = int(
                    con.execute(
                        text("""
                            SELECT COUNT_BIG(*)
                            FROM dbo.etl_drug_event_route
                            WHERE source_domain = :family
                        """),
                        {"family": family},
                    ).scalar_one()
                )
                if n != expected:
                    raise RuntimeError(
                        f"{family} route count changed: "
                        f"{n:,} != {expected:,}"
                    )

            for family, cid in TYPE_CONCEPTS.items():
                row = con.execute(
                    text("""
                        SELECT
                            concept_id,
                            domain_id,
                            invalid_reason
                        FROM dbo.concept
                        WHERE concept_id = :cid
                    """),
                    {"cid": cid},
                ).one()

                if row[1] not in ("Type Concept", "Drug Type"):
                    raise RuntimeError(
                        f"{family} type concept {cid} has unexpected "
                        f"domain {row[1]!r}"
                    )
                if row[2] is not None:
                    raise RuntimeError(
                        f"{family} type concept {cid} is invalid"
                    )

            con.execute(
                text(f"""
                    CREATE TABLE dbo.{XWALK_TABLE} (
                        drug_exposure_id int NOT NULL PRIMARY KEY,
                        route_id bigint NOT NULL UNIQUE,
                        source_domain varchar(32) NOT NULL,
                        source_record_id nvarchar(255) NOT NULL,
                        mapping_basis varchar(32) NOT NULL,
                        date_basis varchar(64) NOT NULL
                    );

                    CREATE INDEX IX_{XWALK_TABLE}_source
                    ON dbo.{XWALK_TABLE} (
                        source_domain,
                        source_record_id
                    );
                """)
            )

            con.execute(
                text(f"""
                    INSERT INTO dbo.{XWALK_TABLE} (
                        drug_exposure_id,
                        route_id,
                        source_domain,
                        source_record_id,
                        mapping_basis,
                        date_basis
                    )
                    SELECT
                        CONVERT(int, route_id),
                        route_id,
                        source_domain,
                        source_record_id,
                        mapping_basis,
                        CASE source_domain
                            WHEN 'PRESCRIBING'
                                THEN 'RX_START_DATE_OR_RX_ORDER_DATE'
                            WHEN 'DISPENSING'
                                THEN 'DISPENSE_DATE'
                            WHEN 'MED_ADMIN'
                                THEN 'MEDADMIN_START_DATE'
                            WHEN 'IMMUNIZATION'
                                THEN 'VX_ADMIN_DATE'
                            WHEN 'PROCEDURES'
                                THEN 'PX_DATE'
                        END
                    FROM dbo.etl_drug_event_route;
                """)
            )

            rx_start = """
                COALESCE(
                    CAST(p.RX_START_DATE AS date),
                    CAST(p.RX_ORDER_DATE AS date)
                )
            """

            rx_start_dt = f"""
                CASE
                    WHEN p.RX_START_DATE IS NOT NULL
                        THEN {_datetime_sql("p.RX_START_DATE")}
                    ELSE {_datetime_sql(
                        "p.RX_ORDER_DATE",
                        "p.RX_ORDER_TIME"
                    )}
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
                        THEN DATEADD(
                            DAY,
                            TRY_CONVERT(int, p.RX_DAYS_SUPPLY) - 1,
                            {rx_start}
                        )
                    ELSE {rx_start}
                END
            """

            con.execute(
                text(f"""
                    INSERT INTO dbo.drug_exposure (
                        drug_exposure_id,
                        person_id,
                        drug_concept_id,
                        drug_exposure_start_date,
                        drug_exposure_start_datetime,
                        drug_exposure_end_date,
                        drug_exposure_end_datetime,
                        verbatim_end_date,
                        drug_type_concept_id,
                        stop_reason,
                        refills,
                        quantity,
                        days_supply,
                        sig,
                        route_concept_id,
                        lot_number,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        drug_source_value,
                        drug_source_concept_id,
                        route_source_value,
                        dose_unit_source_value
                    )
                    SELECT
                        x.drug_exposure_id,
                        pe.person_id,
                        r.target_concept_id,
                        {rx_start},
                        {rx_start_dt},
                        {rx_end},
                        CAST({rx_end} AS datetime),
                        CAST(p.RX_END_DATE AS date),
                        {TYPE_CONCEPTS["PRESCRIBING"]},
                        NULL,
                        TRY_CONVERT(int, p.RX_REFILLS),
                        TRY_CONVERT(float, p.RX_QUANTITY),
                        TRY_CONVERT(int, p.RX_DAYS_SUPPLY),
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(max), p.RAW_RX_FREQUENCY),
                                CONVERT(varchar(max), p.RX_FREQUENCY)
                            ),
                            8000
                        ),
                        0,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50), r.source_code),
                                CONVERT(varchar(50), p.RAW_RX_NDC),
                                CONVERT(varchar(50), p.RXNORM_CUI)
                            ),
                            50
                        ),
                        r.source_concept_id,
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50), p.RAW_RX_ROUTE),
                                CONVERT(varchar(50), p.RX_ROUTE)
                            ),
                            50
                        ),
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50),
                                    p.RAW_RX_DOSE_ORDERED_UNIT),
                                CONVERT(varchar(50),
                                    p.RX_DOSE_ORDERED_UNIT)
                            ),
                            50
                        )
                    FROM dbo.etl_drug_event_route r
                    JOIN dbo.{XWALK_TABLE} x
                      ON x.route_id = r.route_id
                    JOIN dbo.PCORnet_PRESCRIBING p
                      ON LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.PRESCRIBINGID
                         ))) = r.source_record_id
                    JOIN dbo.person pe
                      ON pe.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.PATID
                         )))
                    LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.ENCOUNTERID
                         )))
                    WHERE r.source_domain = 'PRESCRIBING';
                """)
            )

            dispense_end = """
                CASE
                    WHEN TRY_CONVERT(int, d.DISPENSE_SUP) > 0
                        THEN DATEADD(
                            DAY,
                            TRY_CONVERT(int, d.DISPENSE_SUP) - 1,
                            CAST(d.DISPENSE_DATE AS date)
                        )
                    ELSE CAST(d.DISPENSE_DATE AS date)
                END
            """

            con.execute(
                text(f"""
                    INSERT INTO dbo.drug_exposure (
                        drug_exposure_id,
                        person_id,
                        drug_concept_id,
                        drug_exposure_start_date,
                        drug_exposure_start_datetime,
                        drug_exposure_end_date,
                        drug_exposure_end_datetime,
                        verbatim_end_date,
                        drug_type_concept_id,
                        stop_reason,
                        refills,
                        quantity,
                        days_supply,
                        sig,
                        route_concept_id,
                        lot_number,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        drug_source_value,
                        drug_source_concept_id,
                        route_source_value,
                        dose_unit_source_value
                    )
                    SELECT
                        x.drug_exposure_id,
                        pe.person_id,
                        r.target_concept_id,
                        CAST(d.DISPENSE_DATE AS date),
                        {_datetime_sql("d.DISPENSE_DATE")},
                        {dispense_end},
                        CAST({dispense_end} AS datetime),
                        NULL,
                        {TYPE_CONCEPTS["DISPENSING"]},
                        NULL,
                        NULL,
                        TRY_CONVERT(float, d.DISPENSE_AMT),
                        TRY_CONVERT(int, d.DISPENSE_SUP),
                        NULL,
                        0,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        LEFT(CONVERT(varchar(50), d.NDC), 50),
                        r.source_concept_id,
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50), d.RAW_DISPENSE_ROUTE),
                                CONVERT(varchar(50), d.DISPENSE_ROUTE)
                            ),
                            50
                        ),
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50),
                                    d.RAW_DISPENSE_DOSE_DISP_UNIT),
                                CONVERT(varchar(50),
                                    d.DISPENSE_DOSE_DISP_UNIT)
                            ),
                            50
                        )
                    FROM dbo.etl_drug_event_route r
                    JOIN dbo.{XWALK_TABLE} x
                      ON x.route_id = r.route_id
                    JOIN dbo.PCORnet_DISPENSING d
                      ON LTRIM(RTRIM(CONVERT(
                            nvarchar(255), d.DISPENSINGID
                         ))) = r.source_record_id
                    JOIN dbo.person pe
                      ON pe.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), d.PATID
                         )))
                    WHERE r.source_domain = 'DISPENSING';
                """)
            )

            con.execute(
                text(f"""
                    INSERT INTO dbo.drug_exposure (
                        drug_exposure_id,
                        person_id,
                        drug_concept_id,
                        drug_exposure_start_date,
                        drug_exposure_start_datetime,
                        drug_exposure_end_date,
                        drug_exposure_end_datetime,
                        verbatim_end_date,
                        drug_type_concept_id,
                        stop_reason,
                        refills,
                        quantity,
                        days_supply,
                        sig,
                        route_concept_id,
                        lot_number,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        drug_source_value,
                        drug_source_concept_id,
                        route_source_value,
                        dose_unit_source_value
                    )
                    SELECT
                        x.drug_exposure_id,
                        pe.person_id,
                        r.target_concept_id,
                        CAST(m.MEDADMIN_START_DATE AS date),
                        {_datetime_sql(
                            "m.MEDADMIN_START_DATE",
                            "m.MEDADMIN_START_TIME"
                        )},
                        CAST(m.MEDADMIN_STOP_DATE AS date),
                        {_datetime_sql(
                            "m.MEDADMIN_STOP_DATE",
                            "m.MEDADMIN_STOP_TIME"
                        )},
                        CAST(m.MEDADMIN_STOP_DATE AS date),
                        {TYPE_CONCEPTS["MED_ADMIN"]},
                        NULL,
                        NULL,
                        TRY_CONVERT(float, m.MEDADMIN_DOSE_ADMIN),
                        NULL,
                        NULL,
                        0,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50), r.source_code),
                                CONVERT(varchar(50),
                                    m.RAW_MEDADMIN_CODE),
                                CONVERT(varchar(50),
                                    m.MEDADMIN_CODE)
                            ),
                            50
                        ),
                        r.source_concept_id,
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50), m.RAW_MEDADMIN_ROUTE),
                                CONVERT(varchar(50), m.MEDADMIN_ROUTE)
                            ),
                            50
                        ),
                        LEFT(
                            COALESCE(
                                CONVERT(varchar(50),
                                    m.RAW_MEDADMIN_DOSE_ADMIN_UNIT),
                                CONVERT(varchar(50),
                                    m.MEDADMIN_DOSE_ADMIN_UNIT)
                            ),
                            50
                        )
                    FROM dbo.etl_drug_event_route r
                    JOIN dbo.{XWALK_TABLE} x
                      ON x.route_id = r.route_id
                    JOIN dbo.PCORnet_MED_ADMIN m
                      ON LTRIM(RTRIM(CONVERT(
                            nvarchar(255), m.MEDADMINID
                         ))) = r.source_record_id
                    JOIN dbo.person pe
                      ON pe.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), m.PATID
                         )))
                    LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), m.ENCOUNTERID
                         )))
                    WHERE r.source_domain = 'MED_ADMIN';
                """)
            )

            con.execute(
                text(f"""
                    INSERT INTO dbo.drug_exposure (
                        drug_exposure_id,
                        person_id,
                        drug_concept_id,
                        drug_exposure_start_date,
                        drug_exposure_start_datetime,
                        drug_exposure_end_date,
                        drug_exposure_end_datetime,
                        verbatim_end_date,
                        drug_type_concept_id,
                        stop_reason,
                        refills,
                        quantity,
                        days_supply,
                        sig,
                        route_concept_id,
                        lot_number,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        drug_source_value,
                        drug_source_concept_id,
                        route_source_value,
                        dose_unit_source_value
                    )
                    SELECT
                        x.drug_exposure_id,
                        pe.person_id,
                        r.target_concept_id,
                        CAST(i.VX_ADMIN_DATE AS date),
                        {_datetime_sql("i.VX_ADMIN_DATE")},
                        CAST(i.VX_ADMIN_DATE AS date),
                        {_datetime_sql("i.VX_ADMIN_DATE")},
                        CAST(i.VX_ADMIN_DATE AS date),
                        {TYPE_CONCEPTS["IMMUNIZATION"]},
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        0,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), i.VX_CODE), 50),
                        0,
                        LEFT(CONVERT(varchar(50), i.VX_ROUTE), 50),
                        LEFT(CONVERT(varchar(50), i.VX_DOSE_UNIT), 50)
                    FROM dbo.etl_drug_event_route r
                    JOIN dbo.{XWALK_TABLE} x
                      ON x.route_id = r.route_id
                    JOIN dbo.PCORnet_IMMUNIZATION i
                      ON LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.IMMUNIZATIONID
                         ))) = r.source_record_id
                    JOIN dbo.person pe
                      ON pe.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.PATID
                         )))
                    LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.ENCOUNTERID
                         )))
                    WHERE r.source_domain = 'IMMUNIZATION';
                """)
            )

            con.execute(
                text(f"""
                    INSERT INTO dbo.drug_exposure (
                        drug_exposure_id,
                        person_id,
                        drug_concept_id,
                        drug_exposure_start_date,
                        drug_exposure_start_datetime,
                        drug_exposure_end_date,
                        drug_exposure_end_datetime,
                        verbatim_end_date,
                        drug_type_concept_id,
                        stop_reason,
                        refills,
                        quantity,
                        days_supply,
                        sig,
                        route_concept_id,
                        lot_number,
                        provider_id,
                        visit_occurrence_id,
                        visit_detail_id,
                        drug_source_value,
                        drug_source_concept_id,
                        route_source_value,
                        dose_unit_source_value
                    )
                    SELECT
                        x.drug_exposure_id,
                        pe.person_id,
                        r.target_concept_id,
                        CAST(p.PX_DATE AS date),
                        {_datetime_sql("p.PX_DATE")},
                        CAST(p.PX_DATE AS date),
                        {_datetime_sql("p.PX_DATE")},
                        CAST(p.PX_DATE AS date),
                        {TYPE_CONCEPTS["PROCEDURES"]},
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        0,
                        NULL,
                        NULL,
                        vx.visit_occurrence_id,
                        NULL,
                        LEFT(CONVERT(varchar(50), p.PX), 50),
                        r.source_concept_id,
                        NULL,
                        NULL
                    FROM dbo.etl_drug_event_route r
                    JOIN dbo.{XWALK_TABLE} x
                      ON x.route_id = r.route_id
                    JOIN dbo.PCORnet_PROCEDURES p
                      ON LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.PROCEDURESID
                         ))) = r.source_record_id
                    JOIN dbo.person pe
                      ON pe.person_source_value =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.PATID
                         )))
                    LEFT JOIN dbo.etl_visit_occurrence_xwalk vx
                      ON vx.encounterid =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), p.ENCOUNTERID
                         )))
                    WHERE r.source_domain = 'PROCEDURES';
                """)
            )

            family_counts = {}

            for family, expected in EXPECTED.items():
                n = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM dbo.drug_exposure d
                            JOIN dbo.{XWALK_TABLE} x
                              ON x.drug_exposure_id =
                                 d.drug_exposure_id
                            WHERE x.source_domain = :family
                        """),
                        {"family": family},
                    ).scalar_one()
                )

                family_counts[family] = n

                if n != expected:
                    raise RuntimeError(
                        f"{family} drug_exposure mismatch: "
                        f"{n:,} != {expected:,}"
                    )

            target_rows = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                    """)
                ).scalar_one()
            )

            lineage_rows = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM dbo.{XWALK_TABLE}
                    """)
                ).scalar_one()
            )

            concept_zero = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                        WHERE drug_concept_id = 0
                    """)
                ).scalar_one()
            )

            invalid_start = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                        WHERE drug_exposure_start_date IS NULL
                    """)
                ).scalar_one()
            )

            invalid_end = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                        WHERE drug_exposure_end_date IS NULL
                    """)
                ).scalar_one()
            )

            reversed = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                        WHERE drug_exposure_end_date
                              < drug_exposure_start_date
                    """)
                ).scalar_one()
            )

            visit_linked = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.drug_exposure
                        WHERE visit_occurrence_id IS NOT NULL
                    """)
                ).scalar_one()
            )

            if target_rows != EXPECTED_TOTAL:
                raise RuntimeError(
                    f"Final drug_exposure count mismatch: "
                    f"{target_rows:,} != {EXPECTED_TOTAL:,}"
                )

            if lineage_rows != EXPECTED_TOTAL:
                raise RuntimeError(
                    f"Drug lineage count mismatch: "
                    f"{lineage_rows:,}"
                )

            if concept_zero != EXPECTED_CONCEPT_ZERO:
                raise RuntimeError(
                    f"Drug concept-zero mismatch: "
                    f"{concept_zero:,}"
                )

            if invalid_start != 0:
                raise RuntimeError(
                    f"NULL drug start dates found: {invalid_start:,}"
                )

            if invalid_end != 0:
                raise RuntimeError(
                    f"NULL drug end dates found: {invalid_end:,}"
                )

            if reversed != 0:
                raise RuntimeError(
                    f"Drug end before start found: {reversed:,}"
                )

            prescribing_date_basis = dict(
                con.execute(
                    text("""
                        SELECT
                            CASE
                                WHEN p.RX_START_DATE IS NOT NULL
                                    THEN 'RX_START_DATE'
                                ELSE 'RX_ORDER_DATE_FALLBACK'
                            END AS basis,
                            COUNT_BIG(*) AS n
                        FROM dbo.etl_drug_event_route r
                        JOIN dbo.PCORnet_PRESCRIBING p
                          ON LTRIM(RTRIM(CONVERT(
                                nvarchar(255),
                                p.PRESCRIBINGID
                             ))) = r.source_record_id
                        WHERE r.source_domain='PRESCRIBING'
                        GROUP BY
                            CASE
                                WHEN p.RX_START_DATE IS NOT NULL
                                    THEN 'RX_START_DATE'
                                ELSE 'RX_ORDER_DATE_FALLBACK'
                            END
                    """)
                ).all()
            )

            prescribing_end_basis = dict(
                con.execute(
                    text("""
                        SELECT
                            CASE
                                WHEN p.RX_END_DATE IS NOT NULL
                                 AND CAST(p.RX_END_DATE AS date) >=
                                     COALESCE(
                                         CAST(p.RX_START_DATE AS date),
                                         CAST(p.RX_ORDER_DATE AS date)
                                     )
                                    THEN 'RX_END_DATE'
                                WHEN p.RX_END_DATE IS NOT NULL
                                 AND CAST(p.RX_END_DATE AS date) <
                                     COALESCE(
                                         CAST(p.RX_START_DATE AS date),
                                         CAST(p.RX_ORDER_DATE AS date)
                                     )
                                    THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                                WHEN TRY_CONVERT(
                                        int,
                                        p.RX_DAYS_SUPPLY
                                     ) > 0
                                    THEN 'DAYS_SUPPLY_DERIVED'
                                ELSE 'START_DATE_FALLBACK'
                            END AS basis,
                            COUNT_BIG(*) AS n
                        FROM dbo.etl_drug_event_route r
                        JOIN dbo.PCORnet_PRESCRIBING p
                          ON LTRIM(RTRIM(CONVERT(
                                nvarchar(255),
                                p.PRESCRIBINGID
                             ))) = r.source_record_id
                        WHERE r.source_domain='PRESCRIBING'
                        GROUP BY
                            CASE
                                WHEN p.RX_END_DATE IS NOT NULL
                                 AND CAST(p.RX_END_DATE AS date) >=
                                     COALESCE(
                                         CAST(p.RX_START_DATE AS date),
                                         CAST(p.RX_ORDER_DATE AS date)
                                     )
                                    THEN 'RX_END_DATE'
                                WHEN p.RX_END_DATE IS NOT NULL
                                 AND CAST(p.RX_END_DATE AS date) <
                                     COALESCE(
                                         CAST(p.RX_START_DATE AS date),
                                         CAST(p.RX_ORDER_DATE AS date)
                                     )
                                    THEN 'INVALID_SOURCE_INTERVAL_CLAMPED'
                                WHEN TRY_CONVERT(
                                        int,
                                        p.RX_DAYS_SUPPLY
                                     ) > 0
                                    THEN 'DAYS_SUPPLY_DERIVED'
                                ELSE 'START_DATE_FALLBACK'
                            END
                    """)
                ).all()
            )

            invalid_source_interval_source_events = int(
                con.execute(
                    text("""
                        SELECT COUNT_BIG(*)
                        FROM dbo.PCORnet_PRESCRIBING p
                        WHERE p.RX_END_DATE IS NOT NULL
                          AND CAST(p.RX_END_DATE AS date) <
                              COALESCE(
                                  CAST(p.RX_START_DATE AS date),
                                  CAST(p.RX_ORDER_DATE AS date)
                              )
                    """)
                ).scalar_one()
            )

            invalid_source_interval_route_rows = int(
                prescribing_end_basis.get(
                    "INVALID_SOURCE_INTERVAL_CLAMPED", 0
                )
            )

            if (
                invalid_source_interval_source_events
                != EXPECTED_INVALID_SOURCE_INTERVAL_SOURCE_EVENTS
            ):
                raise RuntimeError(
                    "Unexpected reversed PRESCRIBING source-event count: "
                    f"{invalid_source_interval_source_events:,} != "
                    f"{EXPECTED_INVALID_SOURCE_INTERVAL_SOURCE_EVENTS:,}"
                )

            if (
                invalid_source_interval_route_rows
                != EXPECTED_INVALID_SOURCE_INTERVAL_ROUTE_ROWS
            ):
                raise RuntimeError(
                    "Unexpected reversed PRESCRIBING route-row count: "
                    f"{invalid_source_interval_route_rows:,} != "
                    f"{EXPECTED_INVALID_SOURCE_INTERVAL_ROUTE_ROWS:,}"
                )

        payload = {
            "stage": "drug_exposure_transform",
            "recorded_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "family_counts": family_counts,
            "target_rows": target_rows,
            "lineage_rows": lineage_rows,
            "concept_zero_rows": concept_zero,
            "visit_linked_rows": visit_linked,
            "null_start_date_rows": invalid_start,
            "null_end_date_rows": invalid_end,
            "end_before_start_rows": reversed,
            "prescribing_start_date_basis": prescribing_date_basis,
            "prescribing_end_date_basis": prescribing_end_basis,
            "invalid_source_interval_source_events":
                invalid_source_interval_source_events,
            "invalid_source_interval_route_rows":
                invalid_source_interval_route_rows,
            "status": "matched",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        return {
            **payload,
            "audit_path": str(audit_path),
        }

    finally:
        engine.dispose()
