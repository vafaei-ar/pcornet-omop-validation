from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit PCORnet PROCEDURES source structure before implementing procedure_occurrence ETL."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_etl_config(args.config)

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    table = "PCORnet_PROCEDURES"
    audit_path = config.audit_dir / "procedure_source_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            if not table_exists(connection, source_schema, table):
                raise RuntimeError(f"Required table [{source_schema}].[{table}] does not exist")

            columns = [
                str(row[0])
                for row in connection.execute(
                    text(
                        """
                        SELECT c.name
                        FROM sys.columns c
                        JOIN sys.tables t ON t.object_id = c.object_id
                        JOIN sys.schemas s ON s.schema_id = t.schema_id
                        WHERE s.name = :schema_name AND t.name = :table_name
                        ORDER BY c.column_id
                        """
                    ),
                    {"schema_name": source_schema, "table_name": table},
                ).fetchall()
            ]
            colset = {c.upper(): c for c in columns}

            source_rows = int(
                connection.execute(
                    text(f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}]")
                ).scalar_one()
            )

            def count_where(predicate: str) -> int:
                return int(
                    connection.execute(
                        text(f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}] WHERE {predicate}")
                    ).scalar_one()
                )

            counts: dict[str, int | None] = {}
            for logical, column in [
                ("missing_proceduresid", "PROCEDURESID"),
                ("missing_patid", "PATID"),
                ("missing_px", "PX"),
                ("missing_px_date", "PX_DATE"),
                ("missing_encounterid", "ENCOUNTERID"),
            ]:
                actual = colset.get(column)
                if actual is None:
                    counts[logical] = None
                elif column in {"PROCEDURESID", "PATID", "PX", "ENCOUNTERID"}:
                    counts[logical] = count_where(
                        f"{_q(actual)} IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(max), {_q(actual)}))) = ''"
                    )
                else:
                    counts[logical] = count_where(f"{_q(actual)} IS NULL")

            duplicate_id_groups = None
            if "PROCEDURESID" in colset:
                c = _q(colset["PROCEDURESID"])
                duplicate_id_groups = int(
                    connection.execute(
                        text(
                            f"""
                            SELECT COUNT_BIG(*) FROM (
                              SELECT {c}
                              FROM [{source_schema}].[{table}]
                              WHERE {c} IS NOT NULL
                                AND LTRIM(RTRIM(CONVERT(nvarchar(max), {c}))) <> ''
                              GROUP BY {c}
                              HAVING COUNT_BIG(*) > 1
                            ) d
                            """
                        )
                    ).scalar_one()
                )

            person_linked = None
            if "PATID" in colset and table_exists(connection, target_schema, "person"):
                p = _q(colset["PATID"])
                person_linked = int(
                    connection.execute(
                        text(
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[{table}] s
                            JOIN [{target_schema}].[person] p
                              ON CONVERT(nvarchar(50), s.{p}) = p.person_source_value
                            """
                        )
                    ).scalar_one()
                )

            visit_linked = None
            if "ENCOUNTERID" in colset and table_exists(connection, source_schema, "etl_visit_occurrence_xwalk"):
                e = _q(colset["ENCOUNTERID"])
                visit_linked = int(
                    connection.execute(
                        text(
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM [{source_schema}].[{table}] s
                            JOIN [{source_schema}].[etl_visit_occurrence_xwalk] v
                              ON CONVERT(nvarchar(255), s.{e}) = v.encounterid
                            """
                        )
                    ).scalar_one()
                )

            def distribution(column: str) -> list[dict[str, object]]:
                actual = colset.get(column)
                if actual is None:
                    return []
                c = _q(actual)
                rows = connection.execute(
                    text(
                        f"""
                        SELECT COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {c}))), ''), '(missing)') AS value,
                               COUNT_BIG(*) AS n
                        FROM [{source_schema}].[{table}]
                        GROUP BY COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {c}))), ''), '(missing)')
                        ORDER BY COUNT_BIG(*) DESC, value
                        """
                    )
                ).fetchall()
                return [{"value": str(row[0]), "n": int(row[1])} for row in rows]

            px_type = distribution("PX_TYPE")
            px_source = distribution("PX_SOURCE")

    finally:
        engine.dispose()

    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "procedure_source_audit",
        "source_table": f"{source_schema}.{table}",
        "source_rows": source_rows,
        "columns": columns,
        "counts": counts,
        "duplicate_proceduresid_groups": duplicate_id_groups,
        "person_linked_rows": person_linked,
        "visit_linked_rows": visit_linked,
        "px_type_distribution": px_type,
        "px_source_distribution": px_source,
        "interpretation_note": (
            "Read-only audit used to lock the native PCORnet PROCEDURES eligibility and vocabulary rules "
            "before combining native procedure rows with condition-derived Procedure-domain routes."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"PCORnet PROCEDURES rows: {source_rows:,}")
    print("Columns: " + ", ".join(columns))
    print("Key counts:")
    for key, value in counts.items():
        print(f"  {key}: {'n/a' if value is None else f'{value:,}'}")
    print(f"  duplicate PROCEDURESID groups: {'n/a' if duplicate_id_groups is None else f'{duplicate_id_groups:,}'}")
    print(f"  person-linked rows: {'n/a' if person_linked is None else f'{person_linked:,}'}")
    print(f"  visit-linked rows: {'n/a' if visit_linked is None else f'{visit_linked:,}'}")
    print("PX_TYPE distribution:")
    for row in px_type:
        print(f"  {row['value']}: {row['n']:,}")
    print("PX_SOURCE distribution:")
    for row in px_source:
        print(f"  {row['value']}: {row['n']:,}")
    print(f"Audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
