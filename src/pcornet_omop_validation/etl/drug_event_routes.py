from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_drug_event_route"
SOURCE_TABLES = {
    "PRESCRIBING": ("PCORnet_PRESCRIBING", "PRESCRIBINGID"),
    "DISPENSING": ("PCORnet_DISPENSING", "DISPENSINGID"),
    "MED_ADMIN": ("PCORnet_MED_ADMIN", "MEDADMINID"),
    "IMMUNIZATION": ("PCORnet_IMMUNIZATION", "IMMUNIZATIONID"),
}


def _validated_schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str, params: dict[str, object] | None = None) -> int:
    return int(con.execute(text(sql), params or {}).scalar_one())


def build_drug_event_routes(config: EtlConfig) -> dict[str, object]:
    engine = make_engine(config)
    audit_path = config.audit_dir / "drug_event_routes.json"

    sql_cfg = config.raw["sqlserver"]
    source_schema = _validated_schema(
        sql_cfg.get("source_schema", "dbo"), "source_schema"
    )
    target_schema = _validated_schema(
        sql_cfg.get("target_schema", "dbo"), "target_schema"
    )
    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    try:
        with engine.begin() as con:
            source_required = tuple(x[0] for x in SOURCE_TABLES.values()) + (
                "PCORnet_PROCEDURES",
            )
            target_required = (
                "etl_procedure_event_route",
                "concept",
                "concept_relationship",
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

            source_counts: dict[str, int] = {}
            for family, (table, id_col) in SOURCE_TABLES.items():
                total = _scalar(con, f"SELECT COUNT_BIG(*) FROM {s(table)}")
                missing = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM {s(table)}
                    WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))), '') IS NULL
                    """,
                )
                duplicate_groups = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM (
                        SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))) AS source_record_id
                        FROM {s(table)}
                        WHERE NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col}))), '') IS NOT NULL
                        GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), {id_col})))
                        HAVING COUNT_BIG(*) > 1
                    ) q
                    """,
                )
                if missing or duplicate_groups:
                    raise RuntimeError(
                        f"{family} source identifiers are not unique and complete: "
                        f"missing={missing:,}, duplicate_groups={duplicate_groups:,}"
                    )
                source_counts[family] = total

            procedure_source_events = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(DISTINCT source_procedure_id)
                FROM {t('etl_procedure_event_route')}
                WHERE target_domain = 'Drug'
                """,
            )
            source_counts["PROCEDURES"] = procedure_source_events

            con.execute(
                text(f"""
                    IF OBJECT_ID('{target_schema}.{ROUTE_TABLE}', 'U') IS NOT NULL
                        DROP TABLE {t(ROUTE_TABLE)};

                    CREATE TABLE {t(ROUTE_TABLE)} (
                        route_id bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
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
                    ON {t(ROUTE_TABLE)} (source_domain, source_record_id);
                """)
            )

            # PRESCRIBING: RxNorm first, then NDC fallback, then concept 0.
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(nvarchar(255), PRESCRIBINGID))) AS source_record_id,
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), RXNORM_CUI))), '') AS rxnorm_code,
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), RAW_RX_NDC))), '') AS ndc_code
                        FROM {s('PCORnet_PRESCRIBING')}
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
                        LEFT JOIN {t('concept')} c
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
                        LEFT JOIN {t('concept_relationship')} cr
                          ON cr.concept_id_1 = r.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                        LEFT JOIN {t('concept')} tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                        WHERE (
                               r.standard_concept = 'S'
                           AND r.invalid_reason IS NULL
                           AND r.domain_id = 'Drug'
                        ) OR tgt.concept_id IS NOT NULL
                    ),
                    rx_n AS (
                        SELECT source_record_id, COUNT_BIG(*) AS n_targets
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
                          ON n.source_record_id = r.source_record_id
                        LEFT JOIN {t('concept')} c
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
                        JOIN {t('concept_relationship')} cr
                          ON cr.concept_id_1 = n.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                        JOIN {t('concept')} tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                    ),
                    ndc_n AS (
                        SELECT source_record_id, COUNT_BIG(*) AS n_targets
                        FROM ndc_targets
                        GROUP BY source_record_id
                    )
                    INSERT INTO {t(ROUTE_TABLE)} (
                        source_domain, source_record_id, source_code,
                        source_vocabulary_id, source_concept_id,
                        target_concept_id, mapping_basis, disposition
                    )
                    SELECT
                        'PRESCRIBING', rxt.source_record_id, rxt.source_code,
                        'RxNorm', COALESCE(rxt.source_concept_id, 0),
                        rxt.target_concept_id, 'RXNORM',
                        CASE WHEN rxn.n_targets = 1 THEN 'single' ELSE 'multiple' END
                    FROM rx_targets rxt
                    JOIN rx_n rxn ON rxn.source_record_id = rxt.source_record_id
                    UNION ALL
                    SELECT
                        'PRESCRIBING', nt.source_record_id, nt.source_code,
                        'NDC', COALESCE(nt.source_concept_id, 0),
                        nt.target_concept_id, 'NDC_FALLBACK',
                        CASE WHEN nn.n_targets = 1 THEN 'single' ELSE 'multiple' END
                    FROM ndc_targets nt
                    JOIN ndc_n nn ON nn.source_record_id = nt.source_record_id
                    UNION ALL
                    SELECT
                        'PRESCRIBING', r.source_record_id,
                        COALESCE(r.rxnorm_code, r.ndc_code),
                        CASE
                            WHEN r.rxnorm_code IS NOT NULL THEN 'RxNorm'
                            WHEN r.ndc_code IS NOT NULL THEN 'NDC'
                            ELSE NULL
                        END,
                        COALESCE(
                            CASE WHEN r.rxnorm_code IS NOT NULL
                                 THEN r.source_concept_id
                                 ELSE ndc_src.concept_id END,
                            0
                        ),
                        0,
                        CASE
                            WHEN r.rxnorm_code IS NOT NULL THEN 'RXNORM_UNRESOLVED'
                            WHEN r.ndc_code IS NOT NULL THEN 'NDC_UNRESOLVED'
                            ELSE 'NO_CODE'
                        END,
                        'unresolved'
                    FROM rx r
                    LEFT JOIN rx_n rxn ON rxn.source_record_id = r.source_record_id
                    LEFT JOIN ndc_n nn ON nn.source_record_id = r.source_record_id
                    LEFT JOIN {t('concept')} ndc_src
                      ON ndc_src.vocabulary_id = 'NDC'
                     AND ndc_src.concept_code = r.ndc_code
                    WHERE COALESCE(rxn.n_targets, 0) = 0
                      AND COALESCE(nn.n_targets, 0) = 0;
                """)
            )

            # DISPENSING: NDC.
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(nvarchar(255), d.DISPENSINGID))) AS source_record_id,
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), d.NDC))), '') AS source_code,
                            c.concept_id AS source_concept_id
                        FROM {s('PCORnet_DISPENSING')} d
                        LEFT JOIN {t('concept')} c
                          ON c.vocabulary_id = 'NDC'
                         AND c.concept_code = NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), d.NDC))), '')
                    ),
                    targets AS (
                        SELECT DISTINCT
                            s.source_record_id, s.source_code,
                            s.source_concept_id, tgt.concept_id AS target_concept_id
                        FROM src s
                        JOIN {t('concept_relationship')} cr
                          ON cr.concept_id_1 = s.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                        JOIN {t('concept')} tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                    ),
                    n AS (
                        SELECT source_record_id, COUNT_BIG(*) AS n_targets
                        FROM targets GROUP BY source_record_id
                    )
                    INSERT INTO {t(ROUTE_TABLE)} (
                        source_domain, source_record_id, source_code,
                        source_vocabulary_id, source_concept_id,
                        target_concept_id, mapping_basis, disposition
                    )
                    SELECT
                        'DISPENSING', t0.source_record_id, t0.source_code,
                        'NDC', COALESCE(t0.source_concept_id, 0),
                        t0.target_concept_id, 'NDC',
                        CASE WHEN n.n_targets = 1 THEN 'single' ELSE 'multiple' END
                    FROM targets t0
                    JOIN n ON n.source_record_id = t0.source_record_id
                    UNION ALL
                    SELECT
                        'DISPENSING', s0.source_record_id, s0.source_code,
                        'NDC', COALESCE(s0.source_concept_id, 0), 0,
                        'NDC_UNRESOLVED', 'unresolved'
                    FROM src s0
                    LEFT JOIN n ON n.source_record_id = s0.source_record_id
                    WHERE n.source_record_id IS NULL;
                """)
            )

            # MED_ADMIN: RxNorm.
            con.execute(
                text(f"""
                    WITH src AS (
                        SELECT
                            LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMINID))) AS source_record_id,
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMIN_CODE))), '') AS source_code,
                            c.concept_id AS source_concept_id,
                            c.standard_concept,
                            c.invalid_reason,
                            c.domain_id
                        FROM {s('PCORnet_MED_ADMIN')} m
                        LEFT JOIN {t('concept')} c
                          ON c.vocabulary_id = 'RxNorm'
                         AND c.concept_code = NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), m.MEDADMIN_CODE))), '')
                    ),
                    targets AS (
                        SELECT DISTINCT
                            s0.source_record_id, s0.source_code,
                            s0.source_concept_id,
                            CASE
                                WHEN s0.standard_concept = 'S'
                                 AND s0.invalid_reason IS NULL
                                 AND s0.domain_id = 'Drug'
                                    THEN s0.source_concept_id
                                ELSE tgt.concept_id
                            END AS target_concept_id
                        FROM src s0
                        LEFT JOIN {t('concept_relationship')} cr
                          ON cr.concept_id_1 = s0.source_concept_id
                         AND cr.relationship_id = 'Maps to'
                         AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                        LEFT JOIN {t('concept')} tgt
                          ON tgt.concept_id = cr.concept_id_2
                         AND tgt.standard_concept = 'S'
                         AND tgt.invalid_reason IS NULL
                         AND tgt.domain_id = 'Drug'
                        WHERE (
                               s0.standard_concept = 'S'
                           AND s0.invalid_reason IS NULL
                           AND s0.domain_id = 'Drug'
                        ) OR tgt.concept_id IS NOT NULL
                    ),
                    n AS (
                        SELECT source_record_id, COUNT_BIG(*) AS n_targets
                        FROM targets GROUP BY source_record_id
                    )
                    INSERT INTO {t(ROUTE_TABLE)} (
                        source_domain, source_record_id, source_code,
                        source_vocabulary_id, source_concept_id,
                        target_concept_id, mapping_basis, disposition
                    )
                    SELECT
                        'MED_ADMIN', t0.source_record_id, t0.source_code,
                        'RxNorm', COALESCE(t0.source_concept_id, 0),
                        t0.target_concept_id, 'RXNORM',
                        CASE WHEN n.n_targets = 1 THEN 'single' ELSE 'multiple' END
                    FROM targets t0
                    JOIN n ON n.source_record_id = t0.source_record_id
                    UNION ALL
                    SELECT
                        'MED_ADMIN', s0.source_record_id, s0.source_code,
                        CASE WHEN s0.source_code IS NULL THEN NULL ELSE 'RxNorm' END,
                        COALESCE(s0.source_concept_id, 0), 0,
                        CASE WHEN s0.source_code IS NULL THEN 'NO_CODE'
                             ELSE 'RXNORM_UNRESOLVED' END,
                        'unresolved'
                    FROM src s0
                    LEFT JOIN n ON n.source_record_id = s0.source_record_id
                    WHERE n.source_record_id IS NULL;
                """)
            )

            # IMMUNIZATION: reuse procedure Drug routes when linked.
            con.execute(
                text(f"""
                    INSERT INTO {t(ROUTE_TABLE)} (
                        source_domain, source_record_id, source_code,
                        source_vocabulary_id, source_concept_id,
                        target_concept_id, mapping_basis, disposition
                    )
                    SELECT
                        'IMMUNIZATION',
                        LTRIM(RTRIM(CONVERT(nvarchar(255), i.IMMUNIZATIONID))),
                        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), i.VX_CODE))), ''),
                        'CVX', 0, COALESCE(r.target_concept_id, 0),
                        'PROCEDURE_ROUTE',
                        CASE WHEN COALESCE(r.target_concept_id, 0) = 0
                             THEN 'unresolved' ELSE 'single' END
                    FROM {s('PCORnet_IMMUNIZATION')} i
                    LEFT JOIN {t('etl_procedure_event_route')} r
                      ON r.source_procedure_id = LTRIM(RTRIM(CONVERT(nvarchar(255), i.PROCEDURESID)))
                     AND r.target_domain = 'Drug';
                """)
            )

            # PROCEDURES: reuse the canonical procedure Drug route ledger.
            con.execute(
                text(f"""
                    INSERT INTO {t(ROUTE_TABLE)} (
                        source_domain, source_record_id, source_code,
                        source_vocabulary_id, source_concept_id,
                        target_concept_id, mapping_basis, disposition
                    )
                    SELECT
                        'PROCEDURES', r.source_procedure_id, NULL, NULL,
                        COALESCE(r.source_concept_id, 0),
                        COALESCE(r.target_concept_id, 0),
                        'PROCEDURE_ROUTE',
                        CASE WHEN COALESCE(r.target_concept_id, 0) = 0
                             THEN 'unresolved' ELSE r.disposition END
                    FROM {t('etl_procedure_event_route')} r
                    WHERE r.target_domain = 'Drug';
                """)
            )

            route_counts: dict[str, int] = {}
            unresolved_counts: dict[str, int] = {}
            multiple_source_counts: dict[str, int] = {}
            for domain, expected_source_rows in source_counts.items():
                distinct_sources = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(DISTINCT source_record_id)
                    FROM {t(ROUTE_TABLE)}
                    WHERE source_domain = :domain
                    """,
                    {"domain": domain},
                )
                route_rows = _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)} WHERE source_domain = :domain",
                    {"domain": domain},
                )
                unresolved = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)}
                    WHERE source_domain = :domain AND target_concept_id = 0
                    """,
                    {"domain": domain},
                )
                multiple_sources = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*) FROM (
                        SELECT source_record_id
                        FROM {t(ROUTE_TABLE)}
                        WHERE source_domain = :domain
                        GROUP BY source_record_id
                        HAVING COUNT_BIG(*) > 1
                    ) q
                    """,
                    {"domain": domain},
                )
                if distinct_sources != expected_source_rows:
                    raise RuntimeError(
                        f"{domain} route coverage failed: "
                        f"source={expected_source_rows:,}, routed={distinct_sources:,}"
                    )
                route_counts[domain] = route_rows
                unresolved_counts[domain] = unresolved
                multiple_source_counts[domain] = multiple_sources

            invalid_targets = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t(ROUTE_TABLE)} r
                LEFT JOIN {t('concept')} c ON c.concept_id = r.target_concept_id
                WHERE r.target_concept_id <> 0
                  AND (
                      c.concept_id IS NULL
                      OR c.standard_concept <> 'S'
                      OR c.domain_id <> 'Drug'
                      OR c.invalid_reason IS NOT NULL
                  )
                """,
            )
            if invalid_targets:
                raise RuntimeError(
                    f"Drug route ledger contains {invalid_targets:,} invalid nonzero targets"
                )

            source_total = sum(source_counts.values())
            route_total = _scalar(con, f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)}")
            concept_zero = _scalar(
                con,
                f"SELECT COUNT_BIG(*) FROM {t(ROUTE_TABLE)} WHERE target_concept_id = 0",
            )

        payload = {
            "stage": "drug_event_routes",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "source_counts": source_counts,
            "route_counts": route_counts,
            "multiple_source_counts": multiple_source_counts,
            "unresolved_counts": unresolved_counts,
            "source_events": source_total,
            "route_rows": route_total,
            "one_to_many_expansion": route_total - source_total,
            "concept_zero_rows": concept_zero,
            "invalid_standard_target_rows": invalid_targets,
            "status": "matched",
            "policies": {
                "prescribing": "RxNorm first; NDC fallback only when RxNorm yields no Drug target; unresolved retained as concept 0.",
                "dispensing": "NDC Maps to all active Standard Drug targets; unresolved retained as concept 0.",
                "med_admin": "RxNorm direct Standard Drug or active Maps to targets; unresolved retained as concept 0.",
                "immunization": "Reuse canonical procedure Drug route when PROCEDURESID links; otherwise concept 0.",
                "procedures": "Reuse canonical procedure Drug routes including one-to-many mappings.",
            },
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
