from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_drug_event_route"

EXPECTED_SOURCE_EVENTS = {
    "PRESCRIBING": 23_756_583,
    "DISPENSING": 8_368_404,
    "MED_ADMIN": 13_654_315,
    "IMMUNIZATION": 21_132,
    "PROCEDURES": 1_711_374,
}

EXPECTED_SOURCE_TOTAL = sum(EXPECTED_SOURCE_EVENTS.values())

EXPECTED_UNRESOLVED = {
    "PRESCRIBING": 8_860_574,
    "DISPENSING": 203_017,
    "MED_ADMIN": 8_359_850,
    "IMMUNIZATION": 16_821,
    "PROCEDURES": 29_218,
}

EXPECTED_CONCEPT_ZERO = sum(EXPECTED_UNRESOLVED.values())


def build_drug_event_routes(config: EtlConfig) -> dict[str, object]:
    engine = make_engine(config)
    audit_path = config.audit_dir / "drug_event_routes.json"

    try:
        with engine.begin() as con:
            required = (
                "PCORnet_PRESCRIBING",
                "PCORnet_DISPENSING",
                "PCORnet_MED_ADMIN",
                "PCORnet_IMMUNIZATION",
                "PCORnet_PROCEDURES",
                "etl_procedure_event_route",
                "concept",
                "concept_relationship",
            )

            for table in required:
                if not table_exists(con, "dbo", table):
                    raise RuntimeError(
                        f"Required table dbo.{table} does not exist"
                    )

            con.execute(
                text(f"""
                    IF OBJECT_ID('dbo.{ROUTE_TABLE}', 'U') IS NOT NULL
                        DROP TABLE dbo.{ROUTE_TABLE};

                    CREATE TABLE dbo.{ROUTE_TABLE} (
                        route_id bigint IDENTITY(1,1) NOT NULL
                            PRIMARY KEY,
                        source_domain varchar(32) NOT NULL,
                        source_record_id nvarchar(255) NOT NULL,
                        source_code nvarchar(255) NULL,
                        source_vocabulary_id varchar(20) NULL,
                        source_concept_id int NOT NULL,
                        target_concept_id int NOT NULL,
                        mapping_basis varchar(32) NOT NULL,
                        disposition varchar(32) NOT NULL
                    );

                    CREATE INDEX IX_{ROUTE_TABLE}_source
                    ON dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id
                    );
                """)
            )

            # -------------------------------------------------
            # PRESCRIBING
            #
            # Priority:
            # 1. RxNorm direct standard or active Maps to
            # 2. NDC fallback only when RxNorm yielded no target
            # 3. unresolved -> target concept 0
            # -------------------------------------------------
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(
                                nvarchar(255), PRESCRIBINGID
                            ))) AS source_record_id,
                            NULLIF(LTRIM(RTRIM(CONVERT(
                                nvarchar(255), RXNORM_CUI
                            ))), '') AS rxnorm_code,
                            NULLIF(LTRIM(RTRIM(CONVERT(
                                nvarchar(255), RAW_RX_NDC
                            ))), '') AS ndc_code
                        FROM dbo.PCORnet_PRESCRIBING
                    ),
                    rx AS (
                        SELECT
                            s.source_record_id,
                            s.rxnorm_code,
                            s.ndc_code,
                            c.concept_id AS source_concept_id,
                            c.standard_concept,
                            c.invalid_reason,
                            c.domain_id
                        FROM src s
                        LEFT JOIN dbo.concept c
                          ON c.vocabulary_id = 'RxNorm'
                         AND c.concept_code = s.rxnorm_code
                    ),
                    rx_targets AS (
                        SELECT DISTINCT
                            r.source_record_id,
                            r.rxnorm_code AS source_code,
                            r.source_concept_id,
                            CASE
                                WHEN r.standard_concept = 'S'
                                 AND r.invalid_reason IS NULL
                                 AND r.domain_id = 'Drug'
                                    THEN r.source_concept_id
                                ELSE tgt.concept_id
                            END AS target_concept_id
                        FROM rx r
                        LEFT JOIN dbo.concept_relationship cr
                          ON cr.concept_id_1 =
                             r.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (
                                cr.invalid_reason IS NULL
                             OR cr.invalid_reason = ''
                         )
                        LEFT JOIN dbo.concept tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                        WHERE (
                               r.standard_concept = 'S'
                           AND r.invalid_reason IS NULL
                           AND r.domain_id = 'Drug'
                        )
                           OR tgt.concept_id IS NOT NULL
                    ),
                    rx_n AS (
                        SELECT
                            source_record_id,
                            COUNT_BIG(*) AS n_targets
                        FROM rx_targets
                        GROUP BY source_record_id
                    ),
                    ndc AS (
                        SELECT
                            r.source_record_id,
                            r.ndc_code,
                            c.concept_id AS source_concept_id
                        FROM rx r
                        LEFT JOIN rx_n n
                          ON n.source_record_id =
                             r.source_record_id
                        LEFT JOIN dbo.concept c
                          ON c.vocabulary_id = 'NDC'
                         AND c.concept_code = r.ndc_code
                        WHERE COALESCE(n.n_targets, 0) = 0
                    ),
                    ndc_targets AS (
                        SELECT DISTINCT
                            n.source_record_id,
                            n.ndc_code AS source_code,
                            n.source_concept_id,
                            tgt.concept_id AS target_concept_id
                        FROM ndc n
                        JOIN dbo.concept_relationship cr
                          ON cr.concept_id_1 =
                             n.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (
                                cr.invalid_reason IS NULL
                             OR cr.invalid_reason = ''
                         )
                        JOIN dbo.concept tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                    ),
                    ndc_n AS (
                        SELECT
                            source_record_id,
                            COUNT_BIG(*) AS n_targets
                        FROM ndc_targets
                        GROUP BY source_record_id
                    )

                    INSERT INTO dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id,
                        source_code,
                        source_vocabulary_id,
                        source_concept_id,
                        target_concept_id,
                        mapping_basis,
                        disposition
                    )
                    SELECT
                        'PRESCRIBING',
                        rxt.source_record_id,
                        rxt.source_code,
                        'RxNorm',
                        COALESCE(rxt.source_concept_id, 0),
                        rxt.target_concept_id,
                        'RXNORM',
                        CASE
                            WHEN rxn.n_targets = 1
                                THEN 'single'
                            ELSE 'multiple'
                        END
                    FROM rx_targets rxt
                    JOIN rx_n rxn
                      ON rxn.source_record_id =
                         rxt.source_record_id

                    UNION ALL

                    SELECT
                        'PRESCRIBING',
                        nt.source_record_id,
                        nt.source_code,
                        'NDC',
                        COALESCE(nt.source_concept_id, 0),
                        nt.target_concept_id,
                        'NDC_FALLBACK',
                        CASE
                            WHEN nn.n_targets = 1
                                THEN 'single'
                            ELSE 'multiple'
                        END
                    FROM ndc_targets nt
                    JOIN ndc_n nn
                      ON nn.source_record_id =
                         nt.source_record_id

                    UNION ALL

                    SELECT
                        'PRESCRIBING',
                        r.source_record_id,
                        COALESCE(r.rxnorm_code, r.ndc_code),
                        CASE
                            WHEN r.rxnorm_code IS NOT NULL
                                THEN 'RxNorm'
                            WHEN r.ndc_code IS NOT NULL
                                THEN 'NDC'
                            ELSE NULL
                        END,
                        COALESCE(
                            CASE
                                WHEN r.rxnorm_code IS NOT NULL
                                    THEN r.source_concept_id
                                ELSE ndc_src.concept_id
                            END,
                            0
                        ),
                        0,
                        CASE
                            WHEN r.rxnorm_code IS NOT NULL
                                THEN 'RXNORM_UNRESOLVED'
                            WHEN r.ndc_code IS NOT NULL
                                THEN 'NDC_UNRESOLVED'
                            ELSE 'NO_CODE'
                        END,
                        'unresolved'
                    FROM rx r
                    LEFT JOIN rx_n rxn
                      ON rxn.source_record_id =
                         r.source_record_id
                    LEFT JOIN ndc_n nn
                      ON nn.source_record_id =
                         r.source_record_id
                    LEFT JOIN dbo.concept ndc_src
                      ON ndc_src.vocabulary_id = 'NDC'
                     AND ndc_src.concept_code =
                         r.ndc_code
                    WHERE COALESCE(rxn.n_targets, 0) = 0
                      AND COALESCE(nn.n_targets, 0) = 0;
                """)
            )

            # -------------------------------------------------
            # DISPENSING: NDC
            # -------------------------------------------------
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(
                                nvarchar(255), d.DISPENSINGID
                            ))) AS source_record_id,
                            LTRIM(RTRIM(CONVERT(
                                nvarchar(255), d.NDC
                            ))) AS source_code,
                            c.concept_id AS source_concept_id
                        FROM dbo.PCORnet_DISPENSING d
                        LEFT JOIN dbo.concept c
                          ON c.vocabulary_id = 'NDC'
                         AND c.concept_code =
                             LTRIM(RTRIM(CONVERT(
                                nvarchar(255), d.NDC
                             )))
                    ),
                    targets AS (
                        SELECT DISTINCT
                            s.source_record_id,
                            s.source_code,
                            s.source_concept_id,
                            tgt.concept_id AS target_concept_id
                        FROM src s
                        JOIN dbo.concept_relationship cr
                          ON cr.concept_id_1 =
                             s.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (
                                cr.invalid_reason IS NULL
                             OR cr.invalid_reason = ''
                         )
                        JOIN dbo.concept tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                    ),
                    n AS (
                        SELECT
                            source_record_id,
                            COUNT_BIG(*) AS n_targets
                        FROM targets
                        GROUP BY source_record_id
                    )
                    INSERT INTO dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id,
                        source_code,
                        source_vocabulary_id,
                        source_concept_id,
                        target_concept_id,
                        mapping_basis,
                        disposition
                    )
                    SELECT
                        'DISPENSING',
                        t.source_record_id,
                        t.source_code,
                        'NDC',
                        COALESCE(t.source_concept_id, 0),
                        t.target_concept_id,
                        'NDC',
                        CASE
                            WHEN n.n_targets = 1
                                THEN 'single'
                            ELSE 'multiple'
                        END
                    FROM targets t
                    JOIN n
                      ON n.source_record_id =
                         t.source_record_id

                    UNION ALL

                    SELECT
                        'DISPENSING',
                        s.source_record_id,
                        s.source_code,
                        'NDC',
                        COALESCE(s.source_concept_id, 0),
                        0,
                        'NDC_UNRESOLVED',
                        'unresolved'
                    FROM src s
                    LEFT JOIN n
                      ON n.source_record_id =
                         s.source_record_id
                    WHERE n.source_record_id IS NULL;
                """)
            )

            # -------------------------------------------------
            # MED_ADMIN: RxNorm
            # -------------------------------------------------
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(
                                nvarchar(255), m.MEDADMINID
                            ))) AS source_record_id,
                            NULLIF(LTRIM(RTRIM(CONVERT(
                                nvarchar(255), m.MEDADMIN_CODE
                            ))), '') AS source_code,
                            c.concept_id AS source_concept_id,
                            c.standard_concept,
                            c.invalid_reason,
                            c.domain_id
                        FROM dbo.PCORnet_MED_ADMIN m
                        LEFT JOIN dbo.concept c
                          ON c.vocabulary_id = 'RxNorm'
                         AND c.concept_code =
                             NULLIF(LTRIM(RTRIM(CONVERT(
                                nvarchar(255),
                                m.MEDADMIN_CODE
                             ))), '')
                    ),
                    targets AS (
                        SELECT DISTINCT
                            s.source_record_id,
                            s.source_code,
                            s.source_concept_id,
                            CASE
                                WHEN s.standard_concept = 'S'
                                 AND s.invalid_reason IS NULL
                                 AND s.domain_id = 'Drug'
                                    THEN s.source_concept_id
                                ELSE tgt.concept_id
                            END AS target_concept_id
                        FROM src s
                        LEFT JOIN dbo.concept_relationship cr
                          ON cr.concept_id_1 =
                             s.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (
                                cr.invalid_reason IS NULL
                             OR cr.invalid_reason = ''
                         )
                        LEFT JOIN dbo.concept tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                        WHERE (
                               s.standard_concept = 'S'
                           AND s.invalid_reason IS NULL
                           AND s.domain_id = 'Drug'
                        )
                           OR tgt.concept_id IS NOT NULL
                    ),
                    n AS (
                        SELECT
                            source_record_id,
                            COUNT_BIG(*) AS n_targets
                        FROM targets
                        GROUP BY source_record_id
                    )
                    INSERT INTO dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id,
                        source_code,
                        source_vocabulary_id,
                        source_concept_id,
                        target_concept_id,
                        mapping_basis,
                        disposition
                    )
                    SELECT
                        'MED_ADMIN',
                        t.source_record_id,
                        t.source_code,
                        'RxNorm',
                        COALESCE(t.source_concept_id, 0),
                        t.target_concept_id,
                        'RXNORM',
                        CASE
                            WHEN n.n_targets = 1
                                THEN 'single'
                            ELSE 'multiple'
                        END
                    FROM targets t
                    JOIN n
                      ON n.source_record_id =
                         t.source_record_id

                    UNION ALL

                    SELECT
                        'MED_ADMIN',
                        s.source_record_id,
                        s.source_code,
                        CASE
                            WHEN s.source_code IS NULL
                                THEN NULL
                            ELSE 'RxNorm'
                        END,
                        COALESCE(s.source_concept_id, 0),
                        0,
                        CASE
                            WHEN s.source_code IS NULL
                                THEN 'NO_CODE'
                            ELSE 'RXNORM_UNRESOLVED'
                        END,
                        'unresolved'
                    FROM src s
                    LEFT JOIN n
                      ON n.source_record_id =
                         s.source_record_id
                    WHERE n.source_record_id IS NULL;
                """)
            )

            # -------------------------------------------------
            # IMMUNIZATION:
            # reuse frozen procedure Drug route.
            # -------------------------------------------------
            con.execute(
                text(f"""
                    INSERT INTO dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id,
                        source_code,
                        source_vocabulary_id,
                        source_concept_id,
                        target_concept_id,
                        mapping_basis,
                        disposition
                    )
                    SELECT
                        'IMMUNIZATION',
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.IMMUNIZATIONID
                        ))),
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.VX_CODE
                        ))),
                        'CVX',
                        0,
                        COALESCE(r.target_concept_id, 0),
                        'PROCEDURE_ROUTE',
                        CASE
                            WHEN COALESCE(
                                r.target_concept_id, 0
                            ) = 0
                                THEN 'unresolved'
                            ELSE 'single'
                        END
                    FROM dbo.PCORnet_IMMUNIZATION i
                    LEFT JOIN dbo.etl_procedure_event_route r
                      ON r.source_procedure_id =
                         LTRIM(RTRIM(CONVERT(
                            nvarchar(255), i.PROCEDURESID
                         )))
                     AND r.target_domain = 'Drug';
                """)
            )

            # -------------------------------------------------
            # PROCEDURES:
            # copy already-frozen Drug route ledger.
            # -------------------------------------------------
            con.execute(
                text(f"""
                    INSERT INTO dbo.{ROUTE_TABLE} (
                        source_domain,
                        source_record_id,
                        source_code,
                        source_vocabulary_id,
                        source_concept_id,
                        target_concept_id,
                        mapping_basis,
                        disposition
                    )
                    SELECT
                        'PROCEDURES',
                        r.source_procedure_id,
                        NULL,
                        NULL,
                        COALESCE(r.source_concept_id, 0),
                        COALESCE(r.target_concept_id, 0),
                        'PROCEDURE_ROUTE',
                        CASE
                            WHEN COALESCE(
                                r.target_concept_id, 0
                            ) = 0
                                THEN 'unresolved'
                            ELSE r.disposition
                        END
                    FROM dbo.etl_procedure_event_route r
                    WHERE r.target_domain = 'Drug';
                """)
            )

            # -------------------------------------------------
            # Reconciliation
            # -------------------------------------------------
            source_counts = {}
            route_counts = {}
            unresolved_counts = {}
            multiple_source_counts = {}

            for domain in EXPECTED_SOURCE_EVENTS:
                source_rows = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(DISTINCT source_record_id)
                            FROM dbo.{ROUTE_TABLE}
                            WHERE source_domain = :domain
                        """),
                        {"domain": domain},
                    ).scalar_one()
                )

                route_rows = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM dbo.{ROUTE_TABLE}
                            WHERE source_domain = :domain
                        """),
                        {"domain": domain},
                    ).scalar_one()
                )

                unresolved = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM dbo.{ROUTE_TABLE}
                            WHERE source_domain = :domain
                              AND target_concept_id = 0
                        """),
                        {"domain": domain},
                    ).scalar_one()
                )

                multiple_sources = int(
                    con.execute(
                        text(f"""
                            SELECT COUNT_BIG(*)
                            FROM (
                                SELECT source_record_id
                                FROM dbo.{ROUTE_TABLE}
                                WHERE source_domain = :domain
                                GROUP BY source_record_id
                                HAVING COUNT_BIG(*) > 1
                            ) q
                        """),
                        {"domain": domain},
                    ).scalar_one()
                )

                source_counts[domain] = source_rows
                route_counts[domain] = route_rows
                unresolved_counts[domain] = unresolved
                multiple_source_counts[domain] = multiple_sources

                if source_rows != EXPECTED_SOURCE_EVENTS[domain]:
                    raise RuntimeError(
                        f"{domain} source reconciliation failed: "
                        f"{source_rows:,} != "
                        f"{EXPECTED_SOURCE_EVENTS[domain]:,}"
                    )

                if unresolved != EXPECTED_UNRESOLVED[domain]:
                    raise RuntimeError(
                        f"{domain} unresolved reconciliation failed: "
                        f"{unresolved:,} != "
                        f"{EXPECTED_UNRESOLVED[domain]:,}"
                    )

            source_total = sum(source_counts.values())

            route_total = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM dbo.{ROUTE_TABLE}
                    """)
                ).scalar_one()
            )

            concept_zero = int(
                con.execute(
                    text(f"""
                        SELECT COUNT_BIG(*)
                        FROM dbo.{ROUTE_TABLE}
                        WHERE target_concept_id = 0
                    """)
                ).scalar_one()
            )

            if source_total != EXPECTED_SOURCE_TOTAL:
                raise RuntimeError(
                    "Drug source total mismatch: "
                    f"{source_total:,} != "
                    f"{EXPECTED_SOURCE_TOTAL:,}"
                )

            if concept_zero != EXPECTED_CONCEPT_ZERO:
                raise RuntimeError(
                    "Drug concept-zero mismatch: "
                    f"{concept_zero:,} != "
                    f"{EXPECTED_CONCEPT_ZERO:,}"
                )

        payload = {
            "stage": "drug_event_routes",
            "recorded_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "source_counts": source_counts,
            "route_counts": route_counts,
            "multiple_source_counts": multiple_source_counts,
            "unresolved_counts": unresolved_counts,
            "source_events": source_total,
            "route_rows": route_total,
            "one_to_many_expansion": route_total - source_total,
            "concept_zero_rows": concept_zero,
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
