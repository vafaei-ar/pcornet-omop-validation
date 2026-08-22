from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


SOURCE_TABLES = ("PCORnet_DEATH", "PCORnet_DEATH_CAUSE")


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _columns(connection, schema: str, table: str) -> list[dict[str, object]]:
    rows = connection.execute(
        text(
            """
            SELECT
                c.name,
                t.name,
                CASE WHEN c.max_length < 0 THEN NULL ELSE c.max_length END,
                c.is_nullable,
                c.column_id
            FROM sys.columns c
            JOIN sys.types t
              ON t.user_type_id = c.user_type_id
            WHERE c.object_id = OBJECT_ID(:obj)
            ORDER BY c.column_id
            """
        ),
        {"obj": f"{schema}.{table}"},
    ).fetchall()
    return [
        {
            "column_name": str(row[0]),
            "data_type": str(row[1]),
            "max_length": int(row[2]) if row[2] is not None else None,
            "is_nullable": bool(row[3]),
            "ordinal": int(row[4]),
        }
        for row in rows
    ]


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def _column_profile(
    connection,
    schema: str,
    table: str,
    columns: list[dict[str, object]],
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for col in columns:
        name = str(col["column_name"])
        qname = _q(name)
        row = connection.execute(
            text(
                f"""
                SELECT
                    COUNT_BIG(*) AS total_rows,
                    SUM(CASE WHEN {qname} IS NULL THEN 1 ELSE 0 END) AS null_rows,
                    COUNT_BIG(DISTINCT CONVERT(nvarchar(4000), {qname})) AS distinct_nonnull
                FROM [{schema}].[{table}]
                """
            )
        ).one()
        out[name] = {
            "total_rows": int(row[0]),
            "null_rows": int(row[1] or 0),
            "nonnull_rows": int(row[0]) - int(row[1] or 0),
            "distinct_nonnull": int(row[2] or 0),
        }
    return out


def _top_values(
    connection,
    schema: str,
    table: str,
    column: str,
    limit: int = 50,
) -> list[dict[str, object]]:
    qname = _q(column)
    rows = connection.execute(
        text(
            f"""
            SELECT TOP ({int(limit)})
                CONVERT(nvarchar(4000), {qname}) AS value,
                COUNT_BIG(*) AS n
            FROM [{schema}].[{table}]
            GROUP BY CONVERT(nvarchar(4000), {qname})
            ORDER BY n DESC, value
            """
        )
    ).fetchall()
    return [
        {"value": row[0], "n": int(row[1])}
        for row in rows
    ]


def audit_death_planning(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "death_planning_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            for table in SOURCE_TABLES:
                if not table_exists(connection, source_schema, table):
                    raise RuntimeError(
                        f"Required table [{source_schema}].[{table}] does not exist"
                    )
            for table in ("person", "death", "concept", "concept_relationship"):
                if not table_exists(connection, target_schema, table):
                    raise RuntimeError(
                        f"Required table [{target_schema}].[{table}] does not exist"
                    )

            source: dict[str, dict[str, object]] = {}
            for table in SOURCE_TABLES:
                columns = _columns(connection, source_schema, table)
                names = [str(c["column_name"]) for c in columns]
                row_count = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}]",
                )

                patid_profile = None
                if "PATID" in names:
                    patid_profile = {
                        "distinct_nonnull": _scalar(
                            connection,
                            f"""
                            SELECT COUNT_BIG(DISTINCT CONVERT(nvarchar(255), PATID))
                            FROM [{source_schema}].[{table}]
                            WHERE PATID IS NOT NULL
                            """,
                        ),
                        "null_rows": _scalar(
                            connection,
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[{table}]
                            WHERE PATID IS NULL
                            """,
                        ),
                        "unlinked_person_rows": _scalar(
                            connection,
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[{table}] s
                            LEFT JOIN [{target_schema}].[person] p
                              ON p.person_source_value =
                                 LTRIM(RTRIM(CONVERT(nvarchar(255), s.PATID)))
                            WHERE s.PATID IS NOT NULL
                              AND p.person_id IS NULL
                            """,
                        ),
                    }

                categorical_names = [
                    name
                    for name in names
                    if any(
                        token in name.upper()
                        for token in (
                            "SOURCE",
                            "TYPE",
                            "CONFIDENCE",
                            "IMPUTE",
                            "STATUS",
                        )
                    )
                ]

                source[table] = {
                    "row_count": row_count,
                    "columns": columns,
                    "column_profile": _column_profile(
                        connection,
                        source_schema,
                        table,
                        columns,
                    ),
                    "patid_profile": patid_profile,
                    "categorical_top_values": {
                        name: _top_values(
                            connection,
                            source_schema,
                            table,
                            name,
                        )
                        for name in categorical_names
                    },
                }

            death_columns = _columns(connection, target_schema, "death")
            target_death_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death]",
            )

            overlap = None
            death_names = {
                str(c["column_name"])
                for c in source["PCORnet_DEATH"]["columns"]
            }
            cause_names = {
                str(c["column_name"])
                for c in source["PCORnet_DEATH_CAUSE"]["columns"]
            }
            if "PATID" in death_names and "PATID" in cause_names:
                overlap = {
                    "death_patients_with_cause": _scalar(
                        connection,
                        f"""
                        SELECT COUNT_BIG(DISTINCT CONVERT(nvarchar(255), d.PATID))
                        FROM [{source_schema}].[PCORnet_DEATH] d
                        JOIN [{source_schema}].[PCORnet_DEATH_CAUSE] c
                          ON LTRIM(RTRIM(CONVERT(nvarchar(255), c.PATID))) =
                             LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                        WHERE d.PATID IS NOT NULL
                        """,
                    ),
                    "cause_patients_without_death": _scalar(
                        connection,
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM (
                            SELECT DISTINCT
                                LTRIM(RTRIM(CONVERT(nvarchar(255), c.PATID))) AS patid
                            FROM [{source_schema}].[PCORnet_DEATH_CAUSE] c
                            WHERE c.PATID IS NOT NULL
                        ) c
                        LEFT JOIN (
                            SELECT DISTINCT
                                LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) AS patid
                            FROM [{source_schema}].[PCORnet_DEATH] d
                            WHERE d.PATID IS NOT NULL
                        ) d
                          ON d.patid = c.patid
                        WHERE d.patid IS NULL
                        """,
                    ),
                }

            type_candidates = [
                {
                    "concept_id": int(row[0]),
                    "concept_name": str(row[1]),
                    "vocabulary_id": str(row[2]),
                    "concept_class_id": str(row[3]),
                    "standard_concept": row[4],
                    "invalid_reason": row[5],
                }
                for row in connection.execute(
                    text(
                        f"""
                        SELECT
                            concept_id,
                            concept_name,
                            vocabulary_id,
                            concept_class_id,
                            standard_concept,
                            invalid_reason
                        FROM [{target_schema}].[concept]
                        WHERE domain_id = 'Type Concept'
                          AND invalid_reason IS NULL
                          AND (
                               vocabulary_id = 'Death Type'
                            OR concept_name LIKE '%death%'
                            OR concept_name LIKE '%deceased%'
                            OR concept_name LIKE '%expired%'
                          )
                        ORDER BY concept_id
                        """
                    )
                ).fetchall()
            ]

        payload = {
            "stage": "death_planning_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "source_overlap": overlap,
            "target": {
                "death_rows": target_death_rows,
                "death_columns": death_columns,
            },
            "type_concept_candidates": type_candidates,
            "status": "audited",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
