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


def build_environment(config_path: str, driver_folder: str) -> dict[str, str]:
    cfg = load_etl_config(config_path)
    raw = cfg.raw
    sql = raw["sqlserver"]

    if not str(raw["etl"]["cdm_version"]).startswith("5.4"):
        raise ValueError("The preflight is locked to OMOP CDM v5.4.x")
    if str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise ValueError("Expected frozen publication CDM version is 5.4.2")
    if raw["etl"]["backend"] != "sqlserver":
        raise ValueError("Only SQL Server is supported")
    password_env = str(sql.get("password_env", "OMOP_SQL_PASSWORD"))
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
    env.update({
        "OHDSI_DB_SERVER": str(sql["server"]),
        "OHDSI_DB_NAME": str(sql["database"]),
        "OHDSI_TARGET_SCHEMA": str(sql.get("target_schema", "dbo")),
        "OHDSI_DB_USER": str(sql["user"]),
        "OHDSI_DB_PASSWORD": os.environ[password_env],
        "OHDSI_JDBC_DRIVER_PATH": str(path),
        "OHDSI_TLS_TRUST_SERVER_CERTIFICATE": (
            "true" if sql.get("trust_server_certificate", False) is True else "false"
        ),
    })
    return env


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only frozen OMOP SQL Server JDBC preflight")
    p.add_argument("--config", default="config/etl_A.yaml")
    p.add_argument("--driver-dir", default="results/ohdsi_jdbc")
    args = p.parse_args()

    try:
        environment = build_environment(args.config, args.driver_dir)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"PRECHECK BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    script = Path(__file__).with_suffix(".R").resolve()
    if not script.is_file():
        raise SystemExit("PRECHECK BLOCKED: required R script is absent")
    subprocess.run(["Rscript", "--vanilla", str(script)], check=True, env=environment)


if __name__ == "__main__":
    main()
