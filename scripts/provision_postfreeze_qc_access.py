#!/usr/bin/env python3
"""Prepare a dedicated OHDSI QC login and separate SQL Server results database.

DEFAULT: preview only. Applying is a security change and REQUIRES an interactive
confirmation, a new hidden password, and successful frozen-target count checks.

The frozen OMOP tables are queried only. Applying adds a database user and
permissions in the frozen database, and creates a distinct QC output database.
No credentials are stored, printed, uploaded or committed.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import sys

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine

QC_LOGIN = "ohdsi_postfreeze_qc"
QC_DB_SUFFIX = "_postfreeze_qc"
RESULTS_SCHEMA = "qc_results"
SCRATCH_SCHEMA = "qc_scratch"
EXPECTED = {
    "person": 27089,
    "visit_occurrence": 1510957,
    "condition_occurrence": 7315572,
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def checked_identifier(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError("Unsafe SQL Server identifier; use letters, digits and underscores")
    return identifier


def plan_from_config(path: str | Path) -> dict[str, str]:
    cfg = load_etl_config(path)
    raw = cfg.raw
    if str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise ValueError("Expected publication frozen CDM 5.4.2")
    sql = raw["sqlserver"]
    target_db = checked_identifier(str(sql["database"]))
    target_schema = checked_identifier(str(sql.get("target_schema", "dbo")))
    host = str(sql["server"]).split(",")[0].split("\\")[0].lower()
    if host not in {"localhost", "127.0.0.1", ".", "(local)"}:
        raise ValueError("Provisioning is restricted to the known local SQL Server instance")
    qc_db = checked_identifier(target_db + QC_DB_SUFFIX)
    if qc_db.casefold() == target_db.casefold():
        raise ValueError("QC database must differ from the frozen target database")
    if not cfg.sql_password:
        raise ValueError("Existing SQL Server admin password environment variable not set")
    return {
        "target_db": target_db,
        "target_schema": target_schema,
        "qc_db": qc_db,
        "qc_login": QC_LOGIN,
        "results_schema": RESULTS_SCHEMA,
        "scratch_schema": SCRATCH_SCHEMA,
    }


def read_anchors(engine, target_db: str, schema: str) -> None:
    with engine.connect() as conn:
        if str(conn.execute(text("SELECT DB_NAME()")).scalar_one()).casefold() != target_db.casefold():
            raise RuntimeError("Wrong SQL Server database context")
        for table_name, expected in EXPECTED.items():
            # Three-part identifiers below are constructed only from validated
            # identifiers, and table names are source-controlled constants.
            actual = conn.exec_driver_sql(
                f"SELECT COUNT_BIG(*) FROM [{target_db}].[{schema}].[{table_name}]"
            ).scalar_one()
            if int(actual) != expected:
                raise RuntimeError("Frozen table count anchor mismatch")


def password_prompt() -> str:
    a = getpass.getpass("New QC login password (not displayed): ")
    b = getpass.getpass("Confirm new QC login password: ")
    if a != b:
        raise ValueError("Passwords do not match")
    if not (16 <= len(a) <= 128):
        raise ValueError("QC login password must be between 16 and 128 characters")
    if not (any(ch.isupper() for ch in a)
            and any(ch.islower() for ch in a)
            and any(ch.isdigit() for ch in a)
            and any(not ch.isalnum() for ch in a)):
        raise ValueError("Use mixed upper/lower letters, digits and symbols")
    return a


def preflight_admin(master_engine, target_engine, plan: dict[str, str]) -> None:
    target_db = plan["target_db"]
    qc_db = plan["qc_db"]
    with master_engine.connect() as conn:
        role = conn.execute(text("SELECT IS_SRVROLEMEMBER('sysadmin')")).scalar_one()
        if role != 1:
            raise RuntimeError("The connected principal is not authorized to provision SQL logins")
        if conn.execute(
            text("SELECT COUNT(*) FROM sys.server_principals WHERE name=:n"),
            {"n": QC_LOGIN},
        ).scalar_one():
            raise RuntimeError("QC login already exists; review it instead of overwriting")
        if conn.execute(
            text("SELECT COUNT(*) FROM sys.databases WHERE name=:n"), {"n": qc_db}
        ).scalar_one():
            raise RuntimeError("QC database already exists; review it instead of overwriting")
        chaining = conn.execute(
            text("SELECT is_db_chaining_on FROM sys.databases WHERE name=:n"),
            {"n": target_db},
        ).scalar_one()
        if chaining is None or int(chaining) != 0:
            raise RuntimeError("Frozen database missing or cross-database chaining enabled")
    read_anchors(target_engine, target_db, plan["target_schema"])


def provision(master_engine, target_engine, plan: dict[str, str], password: str) -> None:
    db = plan["target_db"]
    schema = plan["target_schema"]
    qcdb = plan["qc_db"]
    user = plan["qc_login"]
    qcres = plan["results_schema"]
    qcscratch = plan["scratch_schema"]
    stage = "starting"
    try:
        # CREATE DATABASE must not run inside a user transaction.
        stage = "create isolated QC output database"
        with master_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(f"CREATE DATABASE [{qcdb}]")

        # The password is passed as an ODBC-bound parameter. It is quoted
        # inside SQL Server and never interpolated into a client SQL string.
        stage = "create dedicated QC login"
        with master_engine.begin() as conn:
            conn.exec_driver_sql(
                f"""
                DECLARE @newpwd nvarchar(128) = ?;
                DECLARE @sql nvarchar(max) =
                   N'CREATE LOGIN [{user}] WITH PASSWORD = ' +
                   QUOTENAME(@newpwd, CHAR(39)) +
                   N', DEFAULT_DATABASE = [{db}], CHECK_POLICY = ON, CHECK_EXPIRATION = OFF';
                EXEC sys.sp_executesql @sql;
                """,
                (password,),
            )

        stage = "grant frozen CDM read-only access"
        # A database user and grants change security metadata, not CDM row data.
        with target_engine.begin() as conn:
            conn.exec_driver_sql(
                f"CREATE USER [{user}] FOR LOGIN [{user}] WITH DEFAULT_SCHEMA = [{schema}]"
            )
            conn.exec_driver_sql(f"GRANT SELECT ON SCHEMA::[{schema}] TO [{user}]")
            conn.exec_driver_sql(
                f"DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[{schema}] TO [{user}]"
            )

        stage = "create restricted results and scratch schemas"
        qc_engine = make_engine(load_etl_config(PLAN_CONFIG_PATH), database=qcdb)
        try:
            with qc_engine.begin() as conn:
                conn.exec_driver_sql(
                    f"CREATE USER [{user}] FOR LOGIN [{user}] WITH DEFAULT_SCHEMA = [{qcres}]"
                )
                conn.exec_driver_sql(f"CREATE SCHEMA [{qcres}] AUTHORIZATION [dbo]")
                conn.exec_driver_sql(f"CREATE SCHEMA [{qcscratch}] AUTHORIZATION [dbo]")
                conn.exec_driver_sql(f"GRANT CREATE TABLE TO [{user}]")
                for qc_schema in (qcres, qcscratch):
                    conn.exec_driver_sql(
                        "GRANT ALTER, SELECT, INSERT, UPDATE, DELETE "
                        f"ON SCHEMA::[{qc_schema}] TO [{user}]"
                    )
        finally:
            qc_engine.dispose()

        stage = "verify frozen row-count anchors after provisioning"
        read_anchors(target_engine, db, schema)
        print("QC provisioning: COMPLETED; original CDM row-count anchors retained")
        print("New QC login:", user)
        print("QC results schema:", f"{qcdb}.{qcres}")
        print("QC scratch schema:", f"{qcdb}.{qcscratch}")
        print("Password not recorded. Save it only in an approved secret manager.")
        print("NEXT: verify this restricted login via read-only JDBC preflight.")
    except Exception:
        # Exception strings from DB drivers may contain connection strings
        # or SQL parameters. Never expose raw errors in terminal output.
        print(
            f"QC provisioning STOPPED during: {stage}. "
            "Partial login/database/schema resources may exist. "
            "Do not rerun or delete them without an administrator review. "
            "Raw database error suppressed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


PLAN_CONFIG_PATH = "config/etl_A.yaml"


def main() -> None:
    global PLAN_CONFIG_PATH
    parser = argparse.ArgumentParser(description="Guarded OHDSI QC SQL Server account provisioning")
    parser.add_argument("--config", default="config/etl_A.yaml")
    parser.add_argument("--apply", action="store_true",
                        help="Apply login and schema changes after explicit interactive authorization")
    args = parser.parse_args()
    PLAN_CONFIG_PATH = args.config

    try:
        plan = plan_from_config(args.config)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"PROVISIONING BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    print("Provisioning scope:")
    print("  frozen database (read-only for QC):", plan["target_db"])
    print("  frozen schema:", plan["target_schema"])
    print("  new login:", plan["qc_login"])
    print("  new, separate database:", plan["qc_db"])
    print("  QC results schema:", plan["results_schema"])
    print("  QC scratch schema:", plan["scratch_schema"])
    print("  existing clinical tables: NO DML/DDL")
    print("  target database security metadata: CREATE USER and GRANT/DENY")
    if not args.apply:
        print("DRY RUN ONLY: no SQL executed. Use --apply after independent review.")
        return
    if not sys.stdin.isatty():
        raise SystemExit("Interactive terminal required for --apply")
    confirmation = input("Type PROVISION POSTFREEZE QC to authorize these changes: ")
    if confirmation != "PROVISION POSTFREEZE QC":
        raise SystemExit("Operator did not authorize provisioning")
    try:
        password = password_prompt()
    except ValueError as exc:
        print(f"PROVISIONING BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    cfg = load_etl_config(args.config)
    master_engine = make_engine(cfg, database="master")
    target_engine = make_engine(cfg)
    try:
        try:
            preflight_admin(master_engine, target_engine, plan)
        except Exception:
            print(
                "PROVISIONING BLOCKED: admin identity, new-resource exclusivity, "
                "cross-database chaining or frozen-target count check failed. "
                "Raw DB error suppressed.",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        provision(master_engine, target_engine, plan, password)
    finally:
        target_engine.dispose()
        master_engine.dispose()


if __name__ == "__main__":
    main()
