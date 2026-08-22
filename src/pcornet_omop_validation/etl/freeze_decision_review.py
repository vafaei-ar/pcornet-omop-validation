from __future__ import annotations

import re
from collections import OrderedDict

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


TYPE_TABLES = (
    ("condition_occurrence", "condition_type_concept_id"),
    ("procedure_occurrence", "procedure_type_concept_id"),
    ("measurement", "measurement_type_concept_id"),
    ("observation", "observation_type_concept_id"),
    ("drug_exposure", "drug_type_concept_id"),
    ("device_exposure", "device_type_concept_id"),
    ("specimen", "specimen_type_concept_id"),
    ("death", "death_type_concept_id"),
)

ROUTE_FIELDS = (
    ("PRESCRIBING", "PCORnet_PRESCRIBING", "RX_ROUTE"),
    ("DISPENSING", "PCORnet_DISPENSING", "DISPENSE_ROUTE"),
    ("MED_ADMIN", "PCORnet_MED_ADMIN", "MEDADMIN_ROUTE"),
    ("IMMUNIZATION", "PCORnet_IMMUNIZATION", "VX_ROUTE"),
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def review_freeze_decisions(config: EtlConfig) -> dict[str, object]:
    """Summarize prespecified semantic decisions that may legitimately retain concept 0."""
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")

    s = lambda table: f"[{source_schema}].[{table}]"
    t = lambda table: f"[{target_schema}].[{table}]"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            type_zero = OrderedDict()
            for table, column in TYPE_TABLES:
                if table_exists(con, target_schema, table):
                    type_zero[table] = _scalar(
                        con,
                        f"SELECT COUNT_BIG(*) FROM {t(table)} "
                        f"WHERE COALESCE({column}, 0)=0",
                    )

            route_profiles: dict[str, object] = {}
            for family, table, column in ROUTE_FIELDS:
                if not table_exists(con, source_schema, table):
                    continue
                rows = con.execute(
                    text(
                        f"""
                        SELECT TOP (30)
                            UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), {column})))) AS route_code,
                            COUNT_BIG(*) AS n
                        FROM {s(table)}
                        WHERE {column} IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(100), {column}))) <> ''
                        GROUP BY UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), {column}))))
                        ORDER BY COUNT_BIG(*) DESC, route_code
                        """
                    )
                ).fetchall()
                route_profiles[family] = [
                    {"route_code": row[0], "n": int(row[1])} for row in rows
                ]

            nonblank_route_zero = _scalar(
                con,
                f"""
                SELECT COUNT_BIG(*)
                FROM {t('drug_exposure')}
                WHERE route_concept_id = 0
                  AND route_source_value IS NOT NULL
                  AND LTRIM(RTRIM(route_source_value)) <> ''
                """,
            )

            distinct_codes: list[str] = []
            for rows in route_profiles.values():
                for row in rows:
                    code = str(row["route_code"])
                    if code not in distinct_codes:
                        distinct_codes.append(code)

            exact_candidates: dict[str, list[dict[str, object]]] = {}
            for code in distinct_codes:
                found = con.execute(
                    text(
                        f"""
                        SELECT TOP (10)
                            concept_id, concept_name, vocabulary_id,
                            concept_code, standard_concept, invalid_reason
                        FROM {t('concept')}
                        WHERE domain_id = 'Route'
                          AND invalid_reason IS NULL
                          AND standard_concept = 'S'
                          AND (
                               UPPER(concept_code) = :code
                            OR UPPER(concept_name) = :code
                          )
                        ORDER BY concept_id
                        """
                    ),
                    {"code": code},
                ).fetchall()
                if found:
                    exact_candidates[code] = [
                        {
                            "concept_id": int(r[0]),
                            "concept_name": str(r[1]),
                            "vocabulary_id": str(r[2]),
                            "concept_code": str(r[3]),
                        }
                        for r in found
                    ]

            return {
                "type_zero_rows": type_zero,
                "type_decision": (
                    "KEEP_ZERO unless a source field or a prespecified source-table semantic "
                    "rule establishes record provenance. Do not blanket-fill a generic EHR "
                    "type merely because the source is PCORnet."
                ),
                "drug_nonblank_route_zero_rows": nonblank_route_zero,
                "route_profiles_top30": route_profiles,
                "exact_standard_route_candidates": exact_candidates,
                "route_decision_rule": (
                    "Map only standardized route codes with a unique exact active Standard "
                    "Route-domain code/name match; leave NI/UN/OT, missing, ambiguous, and "
                    "otherwise unresolved values at concept_id 0."
                ),
                "status": "reviewed",
            }
    finally:
        engine.dispose()
