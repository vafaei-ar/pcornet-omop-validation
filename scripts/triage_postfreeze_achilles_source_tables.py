#!/usr/bin/env python3
"""Read-only OMOP table-presence triage for Achilles categories without results.

Read only: Achilles analysis definitions and analysis-ID existence in the
separate QC output database, SQL Server table names, and EXISTS predicates
over allowlisted frozen OMOP tables. No patient rows, concept values, result
values, or actual row counts are returned to the console.

This diagnoses hypotheses, not omissions conclusively. A nonempty CDM table
with no Achilles result row warrants per-analysis investigation. An empty
derived era table alongside nonempty event records warrants checking whether
the approved ETL was designed to construct eras.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    FROZEN_DATABASE,
    QC_DATABASE,
    inspect_frozen,
    make_restricted_odbc_string,
    quote_sqlserver_name,
    single_scalar,
    verified_server_config,
    verify_identity,
    verify_manifest,
)
from reconcile_postfreeze_achilles_analysis_ids import (  # noqa: E402
    discover_achilles_tables,
)
from summarize_postfreeze_achilles_coverage import (  # noqa: E402
    coverage_sql,
    parse_definition_rows,
    summarize_coverage,
)

# One-to-one CDM source-table association for diagnostic purposes, not a
# claim that every Achilles analysis reads only that table.
CATEGORY_TO_CDM_TABLE = {
    "Visit Detail": "visit_detail",
    "Payer Plan Period": "payer_plan_period",
    "Condition Era": "condition_era",
    "Drug Era": "drug_era",
    "Care Site": "care_site",
    "Location": "location",
    "Provider": "provider",
    "Note": "note",
    "Visit Occurrence": "visit_occurrence",
    "Observation": "observation",
    "Observation Period": "observation_period",
}

# Controls for source event data when the derived era has no result rows.
EVENT_CONTROLS = {
    "condition_occurrence": "condition_era",
    "drug_exposure": "drug_era",
    "person": None,
}
ALLOWLIST = frozenset(CATEGORY_TO_CDM_TABLE.values()) | frozenset(EVENT_CONTROLS)


def actual_frozen_tables(connection) -> dict[str, str]:
    """Resolve real SQL Server table identifiers and reject ambiguity."""
    verify_identity(connection, FROZEN_DATABASE)
    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT t.name
            FROM sys.tables AS t
            JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            WHERE LOWER(s.name) = 'dbo'
        """)
        results = cursor.fetchall()
    finally:
        cursor.close()
    visible: dict[str, str] = {}
    for row in results:
        if row is None or len(row) != 1:
            raise AuditBlocked("Unexpected frozen table metadata response")
        table_name = str(row[0])
        key = table_name.casefold()
        if key in visible:
            raise AuditBlocked("Ambiguous case-equivalent frozen table names")
        visible[key] = table_name
    missing = sorted(table for table in ALLOWLIST if table not in visible)
    if missing:
        raise AuditBlocked("Required frozen CDM table(s) missing: " + ", ".join(missing))
    return {name: visible[name] for name in ALLOWLIST}


def has_any_row(connection, table: str) -> bool:
    """Use EXISTS rather than COUNT_BIG to avoid scanning large tables."""
    if table not in ALLOWLIST:
        raise AuditBlocked("Frozen table-presence query not on approved allowlist")
    sql = (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM [dbo]."
        + quote_sqlserver_name(table)
        + ") THEN 1 ELSE 0 END"
    )
    value = single_scalar(connection, sql)
    if value not in (0, 1):
        raise AuditBlocked("Unexpected frozen table-presence result")
    return value == 1


def source_presence(connection) -> dict[str, bool]:
    actual = actual_frozen_tables(connection)
    return {name: has_any_row(connection, actual[name].casefold())
            for name in sorted(ALLOWLIST)}


def categorize(coverage: dict[str, object], present: dict[str, bool]) -> list[dict[str, object]]:
    if set(present) != ALLOWLIST or any(type(v) is not bool for v in present.values()):
        raise AuditBlocked("Missing or invalid binary source-table presence evidence")
    if coverage.get("defaults_with_data") != 140 or coverage.get("defaults_without_data") != 54:
        raise AuditBlocked("Achilles coverage totals have changed unexpectedly")
    groups = coverage["categories"]
    selected = []
    for category, table in CATEGORY_TO_CDM_TABLE.items():
        if category not in groups:
            raise AuditBlocked("Required Achilles category absent: " + category)
        detail = groups[category]
        no_data, defined, with_data = (
            detail["no_data"], detail["defined"], detail["has_data"]
        )
        if any(type(n) is not int or n < 0 for n in (no_data, defined, with_data)):
            raise AuditBlocked("Invalid Achilles category coverage counts")
        if no_data + with_data != defined:
            raise AuditBlocked("Inconsistent Achilles category summary")
        selected.append({
            "category": category,
            "cdm_table": table,
            "with_data": with_data,
            "without_data": no_data,
            "cdm_nonempty": present[table],
            "interpretation": (
                "SOURCE NONEMPTY; missing-result mechanisms need review"
                if no_data and present[table] else
                "SOURCE EMPTY; consistent with missing result rows, not proof"
                if no_data else
                "No missing-result IDs in category"
            ),
        })
    if sum(x["without_data"] for x in selected) != 54:
        raise AuditBlocked("Targeted categories do not explain all 54 missing-result IDs")
    return sorted(selected, key=lambda row: (-row["without_data"], row["category"]))


def present_report(rows: list[dict[str, object]], present: dict[str, bool]) -> None:
    print("Frozen OMOP source-table presence versus Achilles result-ID coverage")
    print("Source-table state is any-row YES/NO, NOT a patient/event count")
    print("OHDSI category | CDM table | source has rows | Achilles with / without result IDs")
    for row in rows:
        print(
            f"{row['category']} | {row['cdm_table']} | "
            f"{'YES' if row['cdm_nonempty'] else 'NO'} | "
            f"{row['with_data']} / {row['without_data']}"
        )
    print("Reference event-table presence (not record counts):")
    for control in EVENT_CONTROLS:
        print(f"  {control}: {'YES' if present[control] else 'NO'}")
    for event, era in (("condition_occurrence", "condition_era"),
                       ("drug_exposure", "drug_era")):
        if present[event] and not present[era]:
            print(
                f"REVIEW: {era} has no rows despite a populated {event} table. "
                "Check whether the approved ETL includes era construction."
            )
        elif present[event] and present[era]:
            print(
                f"REVIEW: {era} contains rows; missing Achilles results need "
                "analysis-ID-level investigation."
            )
    if any(row["cdm_nonempty"] and row["without_data"] for row in rows):
        print("REVIEW: one or more nonempty OMOP tables have absent Achilles results.")
        print("This does not establish an ETL defect or an OHDSI check failure.")
    print("SOURCE-TABLE TRIAGE: PASS (read-only presence checks complete)")
    print("No patient records, clinical counts, concepts, or strata were retrieved.")


def inspect(config_path: str) -> None:
    verify_manifest()
    cfg = verified_server_config(config_path)
    secret = os.getenv("OHDSI_QC_PASSWORD", "")
    if not secret:
        raise AuditBlocked("OHDSI_QC_PASSWORD missing from the current shell")
    import pyodbc

    qc = pyodbc.connect(
        make_restricted_odbc_string(cfg, QC_DATABASE, secret),
        autocommit=True, timeout=20,
    )
    try:
        verify_identity(qc, QC_DATABASE)
        tables = discover_achilles_tables(qc)
        cursor = qc.cursor()
        try:
            cursor.execute(coverage_sql(tables))
            raw_coverage = cursor.fetchall()
        finally:
            cursor.close()
        coverage = summarize_coverage(parse_definition_rows(raw_coverage))
    finally:
        qc.close()

    frozen = pyodbc.connect(
        make_restricted_odbc_string(cfg, FROZEN_DATABASE, secret),
        autocommit=True, timeout=20,
    )
    try:
        inspect_frozen(frozen)  # Rechecks anchor counts and empty CDM_SOURCE.
        present = source_presence(frozen)
    finally:
        frozen.close()
    rows = categorize(coverage, present)
    present_report(rows, present)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SELECT-only CDM table-presence triage for 54 missing Achilles results"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    args = parser.parse_args()
    try:
        inspect(args.config)
    except AuditBlocked as exc:
        print("SOURCE-TABLE TRIAGE BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Driver diagnostics may contain credentials, so suppress them.
        print("SOURCE-TABLE TRIAGE BLOCKED: SQL provider error suppressed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
