#!/usr/bin/env python3
"""Read-only post-run audit of Achilles result persistence and provenance.

This reads only: (1) a local, ignored execution manifest, (2) SQL Server
metadata/table row counts in the independent QC output database and
(3) unchanged frozen table count anchors. It never selects result values,
concept IDs, clinical records, or row-level data.

Use the dedicated OHDSI_QC_PASSWORD only. It does not use the ETL administrator
login or OMOP_SQL_PASSWORD, does not write SQL, and does not export data.
The printed diagnostics are not a publication/disclosure clearance.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from pcornet_omop_validation.etl.config import load_etl_config

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DATABASE = "OMOP_VALIDATED_A"
FROZEN_SCHEMA = "dbo"
QC_DATABASE = "OMOP_VALIDATED_A_postfreeze_qc"
QC_LOGIN = "ohdsi_postfreeze_qc"
MANIFEST_PATH = ROOT / "results/postfreeze_standard_qc/achilles/execution_manifest.json"
FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
REQUIRED_RESULT_TABLES = (
    "achilles_analysis", "achilles_results", "achilles_results_dist"
)
FROZEN_COUNT_ANCHORS = {
    "person": 27089,
    "visit_occurrence": 1510957,
    "condition_occurrence": 7315572,
}


class AuditBlocked(RuntimeError):
    """Safe operator-facing failure, without provider/secret details."""


def verify_manifest(path: Path = MANIFEST_PATH) -> dict:
    if path.is_symlink() or not path.is_file():
        raise AuditBlocked("Expected local Achilles execution manifest is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AuditBlocked("Cannot read the local Achilles execution manifest") from None
    if not isinstance(payload, dict):
        raise AuditBlocked("Achilles manifest must be a JSON object")
    required = {
        "status": "postfreeze_standard_qc_execution_complete",
        "analysis_role": "exploratory_standard_OMOP_target_only_QC",
        "tool": "achilles",
        "tool_version": "1.7.2",
        "target_cdm_version": "5.4.2",
        "frozen_etl_sha": FROZEN_ETL_SHA,
    }
    for field, expected in required.items():
        if payload.get(field) != expected:
            raise AuditBlocked(f"Achilles manifest mismatch: {field}")
    for field in (
        "target_cdm_counts_preserved",
        "output_local_only",
        "disclosure_review_required_before_publication",
        "cdm_source_metadata_gap_unresolved",
    ):
        if payload.get(field) is not True:
            raise AuditBlocked(f"Achilles manifest missing required safety flag: {field}")
    if payload.get("frozen_cdm_source_row_count") != 0:
        raise AuditBlocked("Achilles manifest must record the empty frozen CDM_SOURCE")
    if payload.get("frozen_cdm_source_populated") is not False:
        raise AuditBlocked("Achilles manifest must not claim CDM_SOURCE is populated")
    if payload.get("achilles_small_cell_threshold") != 10:
        raise AuditBlocked("Achilles manifest must record smallCellCount=10")
    if not isinstance(payload.get("recorded_at_utc"), str) or not payload["recorded_at_utc"]:
        raise AuditBlocked("Achilles manifest has no execution timestamp")
    return payload


def verified_server_config(path: str) -> dict[str, object]:
    cfg = load_etl_config(path)
    raw = cfg.raw
    sql = raw["sqlserver"]
    if raw["etl"]["backend"] != "sqlserver" or str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise AuditBlocked("Unexpected ETL backend/CDM version in local config")
    if str(sql["database"]) != FROZEN_DATABASE:
        raise AuditBlocked("Refusing to inspect an unexpected frozen database")
    if str(sql.get("target_schema", "dbo")) != FROZEN_SCHEMA:
        raise AuditBlocked("Refusing to inspect an unexpected frozen schema")
    host = str(sql["server"]).split(",")[0].split("\\")[0].casefold()
    if host not in {"localhost", "127.0.0.1", ".", "(local)"}:
        raise AuditBlocked("Refusing to inspect a SQL Server outside the known local instance")
    driver = str(sql.get("driver", "ODBC Driver 18 for SQL Server"))
    if not re.fullmatch(r"ODBC Driver (17|18) for SQL Server", driver):
        raise AuditBlocked("Unexpected SQL Server ODBC driver")
    return {
        "server": str(sql["server"]),
        "driver": driver,
        "trust": "yes" if sql.get("trust_server_certificate", False) is True else "no",
    }


def make_restricted_odbc_string(config: dict[str, object], database: str, secret: str) -> str:
    if database not in {FROZEN_DATABASE, QC_DATABASE}:
        raise AuditBlocked("Unexpected database for restricted QC audit")
    if not secret:
        raise AuditBlocked("OHDSI_QC_PASSWORD is missing in the current shell")
    # In ODBC connection strings, brace password values so semicolons stay
    # inside the value, and double a literal closing brace.
    escaped_secret = secret.replace("}", "}}")
    return (
        f"DRIVER={{{config['driver']}}};SERVER={config['server']};"
        f"DATABASE={database};UID={QC_LOGIN};PWD={{{escaped_secret}}};"
        f"Encrypt=yes;TrustServerCertificate={config['trust']};"
    )


def single_scalar(conn, statement: str) -> int:
    if not statement.lstrip().upper().startswith("SELECT "):
        raise AuditBlocked("Only SELECT queries are allowed during inspection")
    cursor = conn.cursor()
    try:
        cursor.execute(statement)
        row = cursor.fetchone()
        if row is None or len(row) != 1:
            raise AuditBlocked("Unexpected query response from SQL Server")
        value = int(row[0])
        if value < 0 or value != row[0]:
            raise AuditBlocked("Unexpected negative/nonintegral SQL row count")
        return value
    finally:
        cursor.close()


def verify_identity(conn, database: str) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DB_NAME(), ORIGINAL_LOGIN()")
        row = cursor.fetchone()
        if (row is None or len(row) != 2 or
                str(row[0]).casefold() != database.casefold() or
                str(row[1]).casefold() != QC_LOGIN.casefold()):
            raise AuditBlocked("SQL Server database or restricted login identity mismatch")
    finally:
        cursor.close()


def quote_sqlserver_name(identifier: str) -> str:
    """Quote an actual server-returned identifier, preserving its exact case."""
    if not identifier or len(identifier) > 128 or "\x00" in identifier:
        raise AuditBlocked("Unexpected SQL Server metadata identifier")
    return "[" + identifier.replace("]", "]]") + "]"


def inspect_results(conn) -> dict[str, int]:
    verify_identity(conn, QC_DATABASE)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT s.name, t.name
            FROM sys.tables AS t
            INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            WHERE LOWER(s.name) IN ('qc_results', 'qc_scratch')
        """)
        rows = cursor.fetchall()
    finally:
        cursor.close()

    # Achilles v1.7.2 creates ACHILLES_ANALYSIS in uppercase on SQL Server,
    # while achilles_results may use a different case. Resolve the *actual*
    # table and schema names instead of assuming Python string-case equality.
    # Fail closed on casefold collisions, missing tables or empty result sets.
    tables: dict[tuple[str, str], tuple[str, str]] = {}
    for raw_schema, raw_name in rows:
        schema, name = str(raw_schema), str(raw_name)
        key = (schema.casefold(), name.casefold())
        if key in tables:
            raise AuditBlocked("Ambiguous case-equivalent Achilles table names")
        tables[key] = (schema, name)

    absent = [
        name for name in REQUIRED_RESULT_TABLES
        if ("qc_results", name.casefold()) not in tables
    ]
    if absent:
        # These are table identifiers, never patient or analytical values.
        visible_names = sorted(
            f"{schema}.{name}" for schema, name in tables.values()
            if schema.casefold() == "qc_results"
        )
        print("QC results schema table names (metadata only):")
        for name in visible_names:
            print(" ", name)
        raise AuditBlocked(
            "Missing core Achilles result table(s): " + ", ".join(absent)
        )

    def actual_table(name: str) -> str:
        schema, table = tables[("qc_results", name.casefold())]
        return (
            f"{quote_sqlserver_name(schema)}."
            f"{quote_sqlserver_name(table)}"
        )

    totals = {}
    for name in REQUIRED_RESULT_TABLES:
        n = single_scalar(
            conn, f"SELECT COUNT_BIG(*) FROM {actual_table(name)}"
        )
        if n == 0:
            raise AuditBlocked(f"Core Achilles results table is empty: {name}")
        totals[name] = n

    ids = single_scalar(conn, f"""
        SELECT COUNT_BIG(*)
        FROM (
            SELECT analysis_id FROM {actual_table("achilles_results")}
            UNION
            SELECT analysis_id FROM {actual_table("achilles_results_dist")}
        ) AS distinct_ids
    """)
    if ids <= 0:
        raise AuditBlocked("No analysis IDs represented in saved Achilles result rows")
    scratch_count = sum(
        1 for schema, _ in tables.values()
        if schema.casefold() == "qc_scratch"
    )
    print("Achilles results inspection: PASS")
    print("Core Achilles result table row counts (not patient counts):")
    for name, n in totals.items():
        print(f"  {name}: {n}")
    print("Distinct analysis IDs represented in stored result rows:", ids)
    print("Permanent QC scratch tables remaining:", scratch_count)
    if scratch_count:
        print("WARNING: inspect remaining scratch tables before any future Achilles execution")
    return totals


def inspect_frozen(conn) -> None:
    verify_identity(conn, FROZEN_DATABASE)
    for table, expected in FROZEN_COUNT_ANCHORS.items():
        actual = single_scalar(conn, f"SELECT COUNT_BIG(*) FROM [dbo].[{table}]")
        if actual != expected:
            raise AuditBlocked(f"Frozen publication row-count anchor mismatch: {table}")
    source_records = single_scalar(conn, "SELECT COUNT_BIG(*) FROM [dbo].[cdm_source]")
    if source_records != 0:
        raise AuditBlocked("Unexpected change to frozen CDM_SOURCE row count")
    print("Frozen publication row-count anchors: PASS")
    print("Frozen CDM_SOURCE: 0 rows, unresolved provenance metadata gap")


def inspect(config_path: str, manifest_path: Path = MANIFEST_PATH) -> None:
    verify_manifest(manifest_path)
    print("Local Achilles manifest: PASS")
    config = verified_server_config(config_path)
    password = os.getenv("OHDSI_QC_PASSWORD", "")
    if not password:
        raise AuditBlocked("OHDSI_QC_PASSWORD is missing; enter it securely in the current shell")
    try:
        import pyodbc
        qc = pyodbc.connect(
            make_restricted_odbc_string(config, QC_DATABASE, password),
            autocommit=True, timeout=20,
        )
        try:
            inspect_results(qc)
        finally:
            qc.close()
        frozen = pyodbc.connect(
            make_restricted_odbc_string(config, FROZEN_DATABASE, password),
            autocommit=True, timeout=20,
        )
        try:
            inspect_frozen(frozen)
        finally:
            frozen.close()
    except AuditBlocked:
        raise
    except Exception:
        # ODBC exceptions can contain connection strings or provider details.
        # Do not log or propagate such values into shared research transcripts.
        raise AuditBlocked("SQL Server inspection failed; provider details suppressed") from None
    print("ACHILLES READ-ONLY POST-RUN AUDIT: PASS")
    print("Results remain local and require disclosure review before any reporting.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="SELECT-only review of saved Achilles results, manifest, and frozen CDM"
    )
    p.add_argument("--config", default="config/etl_A.yaml")
    args = p.parse_args()
    try:
        inspect(args.config)
    except AuditBlocked as exc:
        print("ACHILLES READ-ONLY AUDIT BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
