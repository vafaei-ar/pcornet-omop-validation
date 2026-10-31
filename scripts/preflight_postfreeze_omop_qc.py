#!/usr/bin/env python3
"""Invoke the read-only OHDSI/JDBC preflight using the protected local ETL config.

Never prints or writes credentials. Does not connect to SQL Server itself.
Python passes the existing local password environment variable to R in memory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from pcornet_omop_validation.etl.config import load_etl_config


def build_environment(config_path: str, driver_folder: str, *, qc_login: bool = False) -> dict[str, str]:
    cfg = load_etl_config(config_path)
    raw = cfg.raw
    sql = raw["sqlserver"]

    if not str(raw["etl"]["cdm_version"]).startswith("5.4"):
        raise ValueError("The preflight is locked to OMOP CDM v5.4.x")
    if str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise ValueError("Expected frozen publication CDM version is 5.4.2")
    if raw["etl"]["backend"] != "sqlserver":
        raise ValueError("Only SQL Server is supported")
    password_env = ("OHDSI_QC_PASSWORD" if qc_login else
                    str(sql.get("password_env", "OMOP_SQL_PASSWORD")))
    login_name = "ohdsi_postfreeze_qc" if qc_login else str(sql["user"])
    if not password_env.isidentifier():
        raise ValueError("Unsafe password environment variable name")
    if not os.environ.get(password_env):
        raise ValueError(
            f"Password environment variable {password_env} is unavailable. "
            "Set it only in your local shell or secret manager; never paste it into chat."
        )

    path = Path(driver_folder).resolve()
    if not path.is_dir() or not any(path.rglob("*.jar")):
        raise FileNotFoundError("JDBC driver directory is missing its .jar file")

    env = os.environ.copy()
    if qc_login:
        # Do not pass ETL administrator credentials into an R process that
        # authenticates exclusively as the restricted QC principal.
        env.pop(str(sql.get("password_env", "OMOP_SQL_PASSWORD")), None)
        env.pop("OMOP_SQL_PASSWORD", None)
    env.update({
        "OHDSI_DB_SERVER": str(sql["server"]),
        "OHDSI_DB_NAME": str(sql["database"]),
        "OHDSI_TARGET_SCHEMA": str(sql.get("target_schema", "dbo")),
        "OHDSI_DB_USER": login_name,
        "OHDSI_DB_PASSWORD": os.environ[password_env],
        "OHDSI_JDBC_DRIVER_PATH": str(path),
        "OHDSI_TLS_TRUST_SERVER_CERTIFICATE": (
            "true" if sql.get("trust_server_certificate", False) is True else "false"
        ),
    })
    return env



def diagnose_odbc(config_path: str) -> int:
    """Read-only control check through the existing ETL ODBC connection stack.

    Report only a safe failure category. SQLAlchemy/pyodbc errors may contain
    server names, connection strings or secrets, so never print raw exceptions.
    """
    from sqlalchemy import text
    from pcornet_omop_validation.etl.database import make_engine

    engine = None
    try:
        cfg = load_etl_config(config_path)
        if str(cfg.raw["etl"]["cdm_version"]) != "5.4.2":
            raise ValueError("ODBC diagnostic requires frozen CDM v5.4.2")
        if not cfg.sql_password:
            raise ValueError("Configured password environment variable is missing")
        engine = make_engine(cfg)
        with engine.connect() as con:
            actual_db = str(con.execute(text("SELECT DB_NAME()")).scalar_one())
        expected_db = str(cfg.raw["sqlserver"]["database"])
        if actual_db.casefold() != expected_db.casefold():
            print("ODBC DIAGNOSTIC: connected, but database identity mismatch", file=sys.stderr)
            return 2
        print("ODBC DIAGNOSTIC: connection and database identity PASS")
        if str(cfg.raw["sqlserver"]["user"]).casefold() == "sa":
            print("SA is privileged: this is connectivity only, NOT QC authorization")
        return 0
    except Exception as exc:
        # Inspect in memory solely to classify. Do not print potentially
        # secret-bearing provider exception strings or connection URLs.
        detail = str(exc).lower()
        if "login failed for user" in detail or "18456" in detail or "28000" in detail:
            reason = "SQL Server login rejected"
        elif "driver" in detail or "im002" in detail:
            reason = "ODBC driver issue"
        elif "timeout" in detail or "network" in detail or "08001" in detail:
            reason = "Network or server connectivity issue"
        else:
            reason = "Connection or configuration error"
        print("ODBC DIAGNOSTIC: " + reason + " (raw error suppressed)", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

def main() -> None:
    p = argparse.ArgumentParser(description="Read-only frozen OMOP SQL Server JDBC preflight")
    p.add_argument("--config", default="config/etl_A.yaml")
    p.add_argument("--driver-dir", default="results/ohdsi_jdbc")
    p.add_argument("--diagnose-odbc", action="store_true",
                   help="SELECT-only ODBC connectivity test; do not start JDBC")
    p.add_argument("--qc-login", action="store_true",
                   help="Use only the restricted ohdsi_postfreeze_qc login and OHDSI_QC_PASSWORD")
    p.add_argument("--audit-qc-access", action="store_true",
                   help="SELECT-only audit of all frozen tables and isolated QC output permissions")
    p.add_argument("--inspect-cdm-source", action="store_true",
                   help="SELECT-only diagnosis of CDM_SOURCE metadata, without reading row values")
    args = p.parse_args()

    if args.audit_qc_access and args.inspect_cdm_source:
        raise SystemExit("Choose one of --audit-qc-access or --inspect-cdm-source")
    if (args.audit_qc_access or args.inspect_cdm_source) and not args.qc_login:
        raise SystemExit("Metadata and permission audits require --qc-login")

    if args.diagnose_odbc:
        if args.qc_login:
            raise SystemExit("--qc-login and --diagnose-odbc cannot be combined")
        raise SystemExit(diagnose_odbc(args.config))

    try:
        environment = build_environment(args.config, args.driver_dir,
                                        qc_login=args.qc_login)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"PRECHECK BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    script = (Path(__file__).with_name("audit_postfreeze_qc_permissions.R")
              if args.audit_qc_access else
              Path(__file__).with_name("inspect_postfreeze_cdm_source.R")
              if args.inspect_cdm_source else
              Path(__file__).with_suffix(".R")).resolve()
    if not script.is_file():
        raise SystemExit("PRECHECK BLOCKED: required R script is absent")
    subprocess.run(["Rscript", "--vanilla", str(script)], check=True, env=environment)


if __name__ == "__main__":
    main()
