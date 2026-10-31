#!/usr/bin/env python3
"""Read-only coverage inventory of the saved OHDSI Achilles default analyses.

Source: the isolated QC output database, never the frozen clinical tables.
Reads only analysis_id, is_default, category from ACHILLES_ANALYSIS and
the *presence* of analysis IDs in the two result tables. Never reads the
value, count_value, stratum, concept, or patient columns.

The absence of a result row is NOT itself an Achilles execution failure.
A successful performance record documents execution but does not certify
that a clinically meaningful statistic survived aggregation/suppression.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    QC_DATABASE,
    make_restricted_odbc_string,
    verified_server_config,
    verify_identity,
    verify_manifest,
)
from reconcile_postfreeze_achilles_analysis_ids import (  # noqa: E402
    PERFORMANCE_ID_OFFSET,
    discover_achilles_tables,
)

# The upstream Achilles 1.7.2 analysis details define 194 default and 100
# non-default analyses. These totals are independent of patient data.
EXPECTED_DEFINITIONS = 294
EXPECTED_DEFAULT = 194
EXPECTED_NONDEFAULT = 100
EXPECTED_DEFAULT_DATA = 140
EXPECTED_DEFAULT_PERFORMANCE = 194
EXPECTED_NONDEFAULT_DATA = 0
EXPECTED_NONDEFAULT_PERFORMANCE = 0


def coverage_sql(tables: dict[str, str]) -> str:
    definitions, results, distributions = (
        tables[name] for name in (
            "achilles_analysis", "achilles_results", "achilles_results_dist"
        )
    )
    # Distinct stored IDs keep this one row per *definition* regardless of
    # the number of strata, concept values, or result rows in either table.
    return f"""
    ;WITH stored_ids AS (
      SELECT analysis_id FROM {results} WHERE analysis_id IS NOT NULL
      UNION
      SELECT analysis_id FROM {distributions} WHERE analysis_id IS NOT NULL
    )
    SELECT
      a.analysis_id, a.is_default, a.category,
      CASE WHEN data_id.analysis_id IS NULL THEN 0 ELSE 1 END AS has_data_rows,
      CASE WHEN perf_id.analysis_id IS NULL THEN 0 ELSE 1 END AS has_performance_row
    FROM {definitions} AS a
    LEFT JOIN stored_ids AS data_id ON data_id.analysis_id = a.analysis_id
    LEFT JOIN stored_ids AS perf_id
      ON perf_id.analysis_id = a.analysis_id + {PERFORMANCE_ID_OFFSET}
    ORDER BY a.analysis_id
    """


def parse_definition_rows(rows) -> list[dict[str, object]]:
    if not isinstance(rows, (list, tuple)):
        raise AuditBlocked("Invalid Achilles definition metadata response")
    if len(rows) != EXPECTED_DEFINITIONS:
        raise AuditBlocked(
            f"Unexpected Achilles analysis-definition count: {len(rows)}"
        )
    seen: set[int] = set()
    parsed: list[dict[str, object]] = []
    for row in rows:
        if row is None or len(row) != 5:
            raise AuditBlocked("Malformed Achilles analysis-definition metadata row")
        raw_id, raw_default, raw_category, raw_data, raw_perf = row
        try:
            analysis_id = int(raw_id)
            default = int(raw_default)
            data = int(raw_data)
            performance = int(raw_perf)
        except (ValueError, TypeError, OverflowError):
            raise AuditBlocked("Non-numeric Achilles analysis definition") from None
        if any(
            value != raw for value, raw in (
                (analysis_id, raw_id), (default, raw_default),
                (data, raw_data), (performance, raw_perf),
            )
        ):
            raise AuditBlocked("Invalid nonintegral Achilles definition fields")
        if not (0 <= analysis_id < PERFORMANCE_ID_OFFSET):
            raise AuditBlocked("Unexpected Achilles definition ID namespace")
        if analysis_id in seen:
            raise AuditBlocked("Duplicated Achilles analysis definition ID")
        if default not in (0, 1) or data not in (0, 1) or performance not in (0, 1):
            raise AuditBlocked("Invalid Achilles definition status flag")
        if not isinstance(raw_category, str) or not raw_category.strip():
            raise AuditBlocked("Missing Achilles analysis category")
        if len(raw_category) > 100 or any(
            ord(char) < 32 or ord(char) == 127 for char in raw_category
        ):
            raise AuditBlocked("Unexpected control characters in Achilles category")
        seen.add(analysis_id)
        parsed.append({
            "id": analysis_id,
            "default": default,
            "category": raw_category.strip(),
            "has_data": data,
            "has_performance": performance,
        })
    return parsed


def summarize_coverage(rows: list[dict[str, object]]) -> dict[str, object]:
    default = [record for record in rows if record["default"] == 1]
    nondefault = [record for record in rows if record["default"] == 0]
    if len(rows) != EXPECTED_DEFINITIONS:
        raise AuditBlocked("Achilles metadata definition total changed")
    if len(default) != EXPECTED_DEFAULT or len(nondefault) != EXPECTED_NONDEFAULT:
        raise AuditBlocked("Default versus non-default Achilles definition counts changed")
    data_count = sum(int(record["has_data"]) for record in default)
    performance_count = sum(int(record["has_performance"]) for record in default)
    nd_data = sum(int(record["has_data"]) for record in nondefault)
    nd_perf = sum(int(record["has_performance"]) for record in nondefault)
    if (data_count != EXPECTED_DEFAULT_DATA or
        performance_count != EXPECTED_DEFAULT_PERFORMANCE or
        nd_data != EXPECTED_NONDEFAULT_DATA or
        nd_perf != EXPECTED_NONDEFAULT_PERFORMANCE):
        raise AuditBlocked(
            "Saved Achilles analysis-ID coverage differs from the "
            "previous verified frozen QC snapshot"
        )
    groups = defaultdict(lambda: {"defined": 0, "has_data": 0,
                                  "no_data": 0, "has_performance": 0,
                                  "no_data_ids": []})
    for record in default:
        group = groups[str(record["category"])]
        group["defined"] += 1
        group["has_data"] += int(record["has_data"])
        group["has_performance"] += int(record["has_performance"])
        if not record["has_data"]:
            group["no_data"] += 1
            group["no_data_ids"].append(int(record["id"]))
    if sum(group["no_data"] for group in groups.values()) != 54:
        raise AuditBlocked("Achilles no-data row totals did not reconcile")
    return {
        "categories": dict(groups),
        "defaults_with_data": data_count,
        "defaults_without_data": len(default) - data_count,
        "defaults_with_performance": performance_count,
        "nondefaults_with_data": nd_data,
        "nondefaults_with_performance": nd_perf,
    }


def print_coverage(coverage: dict[str, object], *, show_ids: bool = False) -> None:
    print("Achilles 1.7.2 default-analysis coverage by OHDSI category")
    print("(counts refer to analysis definitions, NOT persons or source records)")
    print(f"Definitions: {EXPECTED_DEFINITIONS}; default: {EXPECTED_DEFAULT}; "
          f"non-default: {EXPECTED_NONDEFAULT}")
    print("Default with stored data-bearing rows:",
          coverage["defaults_with_data"])
    print("Default without stored data-bearing rows:",
          coverage["defaults_without_data"])
    print("Default with saved execution-performance records:",
          coverage["defaults_with_performance"])
    print("Non-default data-bearing/performance records:",
          coverage["nondefaults_with_data"], "/",
          coverage["nondefaults_with_performance"])
    groups = coverage["categories"]
    print("Category | default definitions | with data | without data")
    for name, counts in sorted(groups.items(), key=lambda p: (-p[1]["no_data"], p[0])):
        print(f"{name} | {counts['defined']} | {counts['has_data']} | {counts['no_data']}")
    if show_ids:
        print("Default analysis IDs without data-bearing rows (NOT patient IDs):")
        for name, counts in sorted(groups.items()):
            if counts["no_data_ids"]:
                ids = ", ".join(str(value) for value in sorted(counts["no_data_ids"]))
                print(f"{name}: {ids}")
    print("CATEGORY COVERAGE AUDIT: PASS (194 default, 140 with data, 54 without)")
    print("Interpretation: no result rows is not equivalent to execution failure,")
    print("a quality-check failure, a zero-person domain, or a proven suppression event.")
    print("SELECT-only inspection completed. No clinical counts or strata read.")


def inspect(conn, *, show_ids: bool = False) -> dict[str, object]:
    verify_identity(conn, QC_DATABASE)
    tables = discover_achilles_tables(conn)
    cursor = conn.cursor()
    try:
        cursor.execute(coverage_sql(tables))
        raw_rows = cursor.fetchall()
    finally:
        cursor.close()
    definitions = parse_definition_rows(raw_rows)
    coverage = summarize_coverage(definitions)
    print_coverage(coverage, show_ids=show_ids)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only, metadata-only coverage of saved Achilles analyses"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    parser.add_argument("--show-missing-analysis-ids", action="store_true",
                        help="Show OHDSI analysis IDs, never patient IDs")
    args = parser.parse_args()
    try:
        verify_manifest()
        config = verified_server_config(args.config)
        password = os.getenv("OHDSI_QC_PASSWORD", "")
        if not password:
            raise AuditBlocked("OHDSI_QC_PASSWORD is not set in the current shell")
        import pyodbc
        connection = pyodbc.connect(
            make_restricted_odbc_string(config, QC_DATABASE, password),
            autocommit=True, timeout=20,
        )
        try:
            inspect(connection, show_ids=args.show_missing_analysis_ids)
        finally:
            connection.close()
    except AuditBlocked as exc:
        print("CATEGORY COVERAGE AUDIT BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # SQL driver errors can contain credentials or connection details.
        print("CATEGORY COVERAGE AUDIT BLOCKED: SQL provider error suppressed",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
