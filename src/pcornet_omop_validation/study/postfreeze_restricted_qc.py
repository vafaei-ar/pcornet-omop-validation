"""Importable, fail-closed restricted QC connection guards for model extraction.

Mirrors the established scripts/inspect_postfreeze_achilles.py frozen-target
identity/anchor policy. Keeping this inside the installed src package avoids
assuming scripts/ is importable under pytest's importlib and editable installs.
No clinical record values are retrieved by these checks.
"""
from __future__ import annotations

import re

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_DATABASE = "OMOP_VALIDATED_A"
QC_DATABASE = "OMOP_VALIDATED_A_postfreeze_qc"
FROZEN_SCHEMA = "dbo"
QC_LOGIN = "ohdsi_postfreeze_qc"
FROZEN_COUNT_ANCHORS = {
    "person": 27089,
    "visit_occurrence": 1510957,
    "condition_occurrence": 7315572,
}


class AuditBlocked(RuntimeError):
    """A safe operator-facing failure without provider details."""


def verified_server_config(path: str) -> dict[str, object]:
    cfg = load_etl_config(path)
    raw = cfg.raw
    sql = raw["sqlserver"]
    if raw["etl"]["backend"] != "sqlserver" or str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise AuditBlocked("Unexpected ETL backend/CDM version in local config")
    if str(sql["database"]) != FROZEN_DATABASE:
        raise AuditBlocked("Refusing unexpected frozen database")
    if str(sql.get("target_schema", "dbo")) != FROZEN_SCHEMA:
        raise AuditBlocked("Refusing unexpected frozen schema")
    host = str(sql["server"]).split(",")[0].split("\\")[0].casefold()
    if host not in {"localhost", "127.0.0.1", ".", "(local)"}:
        raise AuditBlocked("Refusing SQL Server outside the locked local instance")
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
        raise AuditBlocked("OHDSI_QC_PASSWORD missing")
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
        if (row is None or len(row) != 2
                or str(row[0]).casefold() != database.casefold()
                or str(row[1]).casefold() != QC_LOGIN.casefold()):
            raise AuditBlocked("SQL database or restricted login identity mismatch")
    finally:
        cursor.close()


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
