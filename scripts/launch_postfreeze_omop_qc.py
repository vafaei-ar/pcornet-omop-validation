#!/usr/bin/env python3
"""Guarded launcher for exploratory post-freeze DQD and Achilles.

The default command is PREVIEW ONLY and does not open database connections.
--execute explicitly requests a local analysis under the dedicated QC login,
never SA. Credentials stay in environment memory and are not printed/saved.
Clinical records and tool output remain on the authorized local workstation.

A full SELECT-only permissions audit runs immediately before each execution.
The R runner independently rechecks database identity, CDM privileges and
frozen reference counts before and after the tool run.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import subprocess

from pcornet_omop_validation.etl.config import load_etl_config

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DATABASE = "OMOP_VALIDATED_A"
FROZEN_SCHEMA = "dbo"
QC_LOGIN = "ohdsi_postfreeze_qc"
PINNED_PACKAGES = {"dqd": "2.9.1", "achilles": "1.7.2"}
CONFIRMATION = "RUN_POSTFREEZE_QC_ON_FROZEN_TARGET"


def validate_target(config_path: str) -> dict[str, str]:
    cfg = load_etl_config(config_path)
    raw = cfg.raw
    sql = raw["sqlserver"]
    db = str(sql["database"])
    schema = str(sql.get("target_schema", "dbo"))
    if str(raw["etl"]["cdm_version"]) != "5.4.2":
        raise ValueError("QC launcher requires frozen OMOP CDM 5.4.2")
    if db != FROZEN_DATABASE or schema != FROZEN_SCHEMA:
        raise ValueError("QC launcher refuses non-frozen database or schema")
    if str(raw["etl"]["backend"]) != "sqlserver":
        raise ValueError("Only SQL Server is supported")
    host = str(sql["server"]).split(",")[0].split("\\")[0].casefold()
    if host not in {"localhost", "127.0.0.1", ".", "(local)"}:
        raise ValueError("QC launcher requires this project's verified local SQL Server")
    old_password_env = str(sql.get("password_env", "OMOP_SQL_PASSWORD"))
    if not old_password_env.isidentifier() or old_password_env == "OHDSI_QC_PASSWORD":
        raise ValueError("Unsafe local ETL password environment variable")
    return {
        "server": str(sql["server"]),
        "database": db,
        "schema": schema,
        "old_password_env": old_password_env,
        "tls_trust": "true" if sql.get("trust_server_certificate", False) is True else "false",
        "qc_database": f"{db}_postfreeze_qc",
    }


def build_environment(
    config: dict[str, str], driver_dir: str, tool: str
) -> dict[str, str]:
    if tool not in PINNED_PACKAGES:
        raise ValueError("Unknown QC tool")
    qc_password = os.getenv("OHDSI_QC_PASSWORD")
    if not qc_password:
        raise ValueError(
            "OHDSI_QC_PASSWORD is not set. Use a local hidden password prompt; "
            "never store the password in config or source code."
        )
    jdbc_folder = Path(driver_dir).resolve()
    if not jdbc_folder.is_dir() or not any(jdbc_folder.rglob("*.jar")):
        raise ValueError("JDBC driver folder is missing a Microsoft SQL Server .jar")
    env = os.environ.copy()
    # The R process needs only the restricted QC credential.
    env.pop(config["old_password_env"], None)
    env.pop("OMOP_SQL_PASSWORD", None)
    env.pop("OHDSI_DB_PASSWORD", None)
    env.update({
        "OHDSI_DB_SERVER": config["server"],
        "OHDSI_DB_NAME": config["database"],
        "OHDSI_TARGET_SCHEMA": config["schema"],
        "OHDSI_DB_USER": QC_LOGIN,
        "OHDSI_DB_PASSWORD": qc_password,
        "OHDSI_JDBC_DRIVER_PATH": str(jdbc_folder),
        "OHDSI_TLS_TRUST_SERVER_CERTIFICATE": config["tls_trust"],
        "OHDSI_CDM_SCHEMA": f'{config["database"]}.{config["schema"]}',
        "OHDSI_RESULTS_SCHEMA": f'{config["qc_database"]}.qc_results',
        "OHDSI_SCRATCH_SCHEMA": f'{config["qc_database"]}.qc_scratch',
        "OHDSI_QC_EXPECTED_PACKAGE_VERSION": PINNED_PACKAGES[tool],
        "OHDSI_QC_CONFIRM": CONFIRMATION,
        "R_LIBS_USER": os.environ.get(
            "R_LIBS_USER", str(ROOT / "results" / "ohdsi_r_lib")
        ),
    })
    return env


def planned_output(tool: str) -> Path:
    if tool not in PINNED_PACKAGES:
        raise ValueError("Unknown QC tool")
    return ROOT / "results" / "postfreeze_standard_qc" / tool


def check_new_output_location(tool: str) -> None:
    target = planned_output(tool)
    if target.is_symlink():
        raise ValueError("Output destination must not be a symlink")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(
            "Previous QC output files already exist. "
            "Inspect and preserve them before any repeat execution."
        )


def run_qc(config_path: str, driver_dir: str, tool: str, *, execute: bool) -> None:
    config = validate_target(config_path)
    print("QC tool:", tool)
    print("Frozen database:", config["database"])
    print("Restricted SQL login:", QC_LOGIN)
    print("QC results schema:", f'{config["qc_database"]}.qc_results')
    print("QC scratch schema:", f'{config["qc_database"]}.qc_scratch')
    print("Expected installed R package version:", PINNED_PACKAGES[tool])
    print("Local output folder:", planned_output(tool))
    print(
        "DQD database output disabled: writeToTable=FALSE"
        if tool == "dqd" else
        "Achilles will write only into the separate QC output database."
    )
    if not execute:
        print("PREVIEW ONLY: no database connection or analysis performed.")
        return
    check_new_output_location(tool)
    env = build_environment(config, driver_dir, tool)
    # Files from DQD/Achilles can contain sensitive aggregate counts.
    # Keep new output files private to this user's local account by default.
    os.umask(0o077)
    print("Running fresh SELECT-only permission audit before analysis...", flush=True)
    subprocess.run(
        ["Rscript", "--vanilla", str(ROOT / "scripts" /
         "audit_postfreeze_qc_permissions.R")],
        check=True, env=env, cwd=ROOT,
    )
    print("Permission audit passed; starting", tool, flush=True)
    subprocess.run(
        ["Rscript", "--vanilla", str(ROOT / "scripts" /
         "run_postfreeze_omop_qc.R"), tool],
        check=True, env=env, cwd=ROOT,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Preview or explicitly run protected local post-freeze OHDSI QC"
    )
    p.add_argument("--config", default="config/etl_A.yaml")
    p.add_argument("--driver-dir", default="results/ohdsi_jdbc")
    p.add_argument("--tool", choices=("dqd", "achilles"), default="dqd")
    p.add_argument("--execute", action="store_true",
                   help="Run the full permissions audit then one OHDSI tool")
    a = p.parse_args()
    try:
        run_qc(a.config, a.driver_dir, a.tool, execute=a.execute)
    except (ValueError, FileNotFoundError) as exc:
        print(f"QC LAUNCH BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError:
        print(
            "QC execution FAILED: review local private logs. "
            "Do not interpret partial output as completed results.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
