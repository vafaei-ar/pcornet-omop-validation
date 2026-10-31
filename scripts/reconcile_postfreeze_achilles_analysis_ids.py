#!/usr/bin/env python3
"""Reconcile persisted Achilles analysis IDs with their metadata definitions.

SELECT-only diagnostic. Uses the existing dedicated QC account and the
ignored, already verified Achilles execution manifest. No person, concept,
stratum, distribution, or count_value fields are read. Optional numeric
analysis IDs printed here are OHDSI analysis identifiers, NOT patient IDs.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    QC_DATABASE,
    make_restricted_odbc_string,
    quote_sqlserver_name,
    verified_server_config,
    verify_identity,
    verify_manifest,
)

REQUIRED_TABLES = ("achilles_analysis", "achilles_results", "achilles_results_dist")
METRICS = (
    "results_ids", "distribution_ids", "combined_ids", "overlap_ids",
    "definition_rows", "definition_ids", "unmatched_results_ids",
    "definitions_without_results", "null_definition_ids",
    "null_result_ids", "null_distribution_ids",
)


def discover_achilles_tables(conn) -> dict[str, str]:
    """Use SQL Server's actual schema/table casing rather than case-sensitive Python names."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT s.name, t.name FROM sys.tables AS t
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            WHERE LOWER(s.name) = 'qc_results'
        """)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    tables: dict[str, str] = {}
    for schema, name in rows:
        key = str(name).casefold()
        if key in tables:
            raise AuditBlocked("Ambiguous case-equivalent QC results table names")
        tables[key] = (
            f"{quote_sqlserver_name(str(schema))}."
            f"{quote_sqlserver_name(str(name))}"
        )
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise AuditBlocked(
            "Cannot reconcile IDs; missing Achilles tables: " + ", ".join(missing)
        )
    return {name: tables[name] for name in REQUIRED_TABLES}


def reconciliation_sql(tables: dict[str, str]) -> str:
    analysis, results, dist = (tables[name] for name in REQUIRED_TABLES)
    return f"""
    ;WITH regular_ids AS (
        SELECT DISTINCT analysis_id FROM {results}
        WHERE analysis_id IS NOT NULL
    ), dist_ids AS (
        SELECT DISTINCT analysis_id FROM {dist}
        WHERE analysis_id IS NOT NULL
    ), result_ids AS (
        SELECT analysis_id FROM regular_ids
        UNION SELECT analysis_id FROM dist_ids
    ), definition_ids AS (
        SELECT DISTINCT analysis_id FROM {analysis}
        WHERE analysis_id IS NOT NULL
    )
    SELECT
        (SELECT COUNT_BIG(*) FROM regular_ids) AS results_ids,
        (SELECT COUNT_BIG(*) FROM dist_ids) AS distribution_ids,
        (SELECT COUNT_BIG(*) FROM result_ids) AS combined_ids,
        (SELECT COUNT_BIG(*) FROM (
            SELECT analysis_id FROM regular_ids
            INTERSECT SELECT analysis_id FROM dist_ids
        ) AS both_ids) AS overlap_ids,
        (SELECT COUNT_BIG(*) FROM {analysis}) AS definition_rows,
        (SELECT COUNT_BIG(*) FROM definition_ids) AS definition_ids,
        (SELECT COUNT_BIG(*) FROM (
            SELECT analysis_id FROM result_ids
            EXCEPT SELECT analysis_id FROM definition_ids
        ) AS unmatched_ids) AS unmatched_results_ids,
        (SELECT COUNT_BIG(*) FROM (
            SELECT analysis_id FROM definition_ids
            EXCEPT SELECT analysis_id FROM result_ids
        ) AS unexecuted_ids) AS definitions_without_results,
        (SELECT COUNT_BIG(*) FROM {analysis} WHERE analysis_id IS NULL)
            AS null_definition_ids,
        (SELECT COUNT_BIG(*) FROM {results} WHERE analysis_id IS NULL)
            AS null_result_ids,
        (SELECT COUNT_BIG(*) FROM {dist} WHERE analysis_id IS NULL)
            AS null_distribution_ids
    """


def unmatched_id_sample_sql(tables: dict[str, str]) -> str:
    analysis, results, dist = (tables[name] for name in REQUIRED_TABLES)
    return f"""
    ;WITH result_ids AS (
        SELECT analysis_id FROM {results} WHERE analysis_id IS NOT NULL
        UNION
        SELECT analysis_id FROM {dist} WHERE analysis_id IS NOT NULL
    ), definition_ids AS (
        SELECT DISTINCT analysis_id FROM {analysis}
        WHERE analysis_id IS NOT NULL
    )
    SELECT TOP (20) analysis_id FROM (
        SELECT analysis_id FROM result_ids
        EXCEPT SELECT analysis_id FROM definition_ids
    ) AS unmatched ORDER BY analysis_id
    """


def validate_reconciliation(values) -> dict[str, int]:
    if values is None or len(values) != len(METRICS):
        raise AuditBlocked("Unexpected Achilles analysis-ID aggregate result")
    try:
        parsed = {k: int(v) for k, v in zip(METRICS, values)}
    except (ValueError, TypeError, OverflowError):
        raise AuditBlocked("Non-numeric Achilles analysis-ID summary") from None
    if any(v < 0 or v != original for v, original in zip(parsed.values(), values)):
        raise AuditBlocked("Invalid Achilles analysis-ID aggregate")
    n = parsed
    if n["combined_ids"] != n["results_ids"] + n["distribution_ids"] - n["overlap_ids"]:
        raise AuditBlocked("Inconsistent union/intersection of Achilles analysis IDs")
    if not 0 <= n["unmatched_results_ids"] <= n["combined_ids"]:
        raise AuditBlocked("Invalid unmatched-result analysis-ID count")
    if not 0 <= n["definitions_without_results"] <= n["definition_ids"]:
        raise AuditBlocked("Invalid unused-definition count")
    if (n["combined_ids"] - n["unmatched_results_ids"] !=
            n["definition_ids"] - n["definitions_without_results"]):
        raise AuditBlocked("Inconsistent overlap between analysis results and metadata")
    if n["definition_rows"] < n["definition_ids"] + n["null_definition_ids"]:
        raise AuditBlocked("Metadata row count smaller than distinct IDs")
    if n["combined_ids"] == 0 or n["definition_ids"] == 0:
        raise AuditBlocked("Missing stored Achilles analysis IDs or definitions")
    return n


def reconcile(conn) -> dict[str, int]:
    verify_identity(conn, QC_DATABASE)
    tables = discover_achilles_tables(conn)
    cursor = conn.cursor()
    try:
        cursor.execute(reconciliation_sql(tables))
        summary = validate_reconciliation(cursor.fetchone())
    finally:
        cursor.close()
    print("Achilles analysis-ID reconciliation (metadata only):")
    print("  Result table distinct IDs:", summary["results_ids"])
    print("  Distribution table distinct IDs:", summary["distribution_ids"])
    print("  IDs in both result tables:", summary["overlap_ids"])
    print("  Combined stored analysis IDs:", summary["combined_ids"])
    print("  Analysis-definition rows:", summary["definition_rows"])
    print("  Distinct defined analysis IDs:", summary["definition_ids"])
    print("  Stored IDs without metadata definitions:", summary["unmatched_results_ids"])
    print("  Defined IDs without stored result rows:", summary["definitions_without_results"])
    print("  Duplicate definition ID rows:",
          summary["definition_rows"] - summary["definition_ids"] -
          summary["null_definition_ids"])
    print("  NULL IDs in definition/regular/distribution:",
          summary["null_definition_ids"], "/",
          summary["null_result_ids"], "/",
          summary["null_distribution_ids"])
    if summary["unmatched_results_ids"]:
        cursor = conn.cursor()
        try:
            cursor.execute(unmatched_id_sample_sql(tables))
            sample = [int(row[0]) for row in cursor.fetchall()]
        finally:
            cursor.close()
        if not sample:
            raise AuditBlocked("Unmatched ID count positive but sample is empty")
        print("  First unmatched OHDSI analysis IDs (NOT patient IDs):",
              ", ".join(str(value) for value in sample))
        print("RECONCILIATION: REVIEW REQUIRED for IDs absent from metadata definitions")
    elif (summary["null_definition_ids"] or summary["null_result_ids"] or
          summary["null_distribution_ids"]):
        print("RECONCILIATION: REVIEW REQUIRED for NULL analysis identifiers")
    else:
        print("RECONCILIATION: all persisted analysis IDs have definitions")
    print("Defined analyses with no result rows may be expected with defaultAnalysesOnly=TRUE.")
    print("DIAGNOSTIC COMPLETE: SELECT only, no data modified or exported")
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="SELECT-only Achilles analysis-ID reconciliation")
    p.add_argument("--config", default="config/etl_A.yaml")
    args = p.parse_args()
    try:
        verify_manifest()
        cfg = verified_server_config(args.config)
        password = os.getenv("OHDSI_QC_PASSWORD", "")
        if not password:
            raise AuditBlocked("OHDSI_QC_PASSWORD missing; set it in a local hidden prompt")
        import pyodbc
        connection = pyodbc.connect(
            make_restricted_odbc_string(cfg, QC_DATABASE, password),
            autocommit=True, timeout=20,
        )
        try:
            reconcile(connection)
        finally:
            connection.close()
    except AuditBlocked as exc:
        print("ACHILLES ID DIAGNOSTIC BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Raw ODBC exceptions may contain sensitive driver or credentials data.
        print("ACHILLES ID DIAGNOSTIC BLOCKED: provider error suppressed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
