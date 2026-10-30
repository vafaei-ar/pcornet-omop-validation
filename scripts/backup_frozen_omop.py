#!/usr/bin/env python3
"""Safely create and verify a COPY_ONLY backup of the frozen OMOP publication DB.

By default this is a no-SQL preview. --execute initiates one backup, writes
to SQL Server's protected backup directory, and verifies the completed media.

Critical pyodbc detail: SQL Server BACKUP/RESTORE produce multiple progress
results. Calling cursor.close() directly after execute() can CANCEL BACKUP.
Drain cursor.nextset() to completion before closing the cursor. See:
https://github.com/mkleehammer/pyodbc/issues/471

Never print connection strings, password values, or raw DB driver exceptions.
Do not store backup files or clinical data in the Git repository.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
import shutil
import sys

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine

FROZEN_DATABASE = "OMOP_VALIDATED_A"
BACKUP_DIRECTORY = Path("/var/opt/mssql/backup")
EXPECTED_ROWS = {
    "person": 27089,
    "visit_occurrence": 1510957,
    "condition_occurrence": 7315572,
}
MIN_FREE_GIB = 170


def build_backup_path(now: datetime | None = None, token: str | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = token if token is not None else token_hex(5)
    if not suffix or any(ch not in "0123456789abcdef" for ch in suffix):
        raise ValueError("Invalid backup filename suffix")
    return BACKUP_DIRECTORY / f"{FROZEN_DATABASE}_copyonly_{stamp}_{suffix}.bak"


def drain_dbapi_results(dbapi_conn, sql: str) -> None:
    """Execute one T-SQL statement and consume ALL results before close.

    Required for BACKUP/RESTORE over pyodbc. The no-results-yet state is
    handled inside nextset() by the ODBC driver.
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute(sql)
        while cursor.nextset():
            pass
    finally:
        cursor.close()


def check_frozen_anchor(conn, database: str, schema: str) -> None:
    observed_db = str(conn.execute(text("SELECT DB_NAME()")).scalar_one())
    if observed_db.casefold() != database.casefold():
        raise RuntimeError("Connected database does not match frozen target")
    if not (schema.isidentifier() and schema.isascii()):
        raise RuntimeError("Unexpected frozen CDM schema identifier")
    for table, expected in EXPECTED_ROWS.items():
        observed = conn.exec_driver_sql(
            f"SELECT COUNT_BIG(*) FROM [{database}].[{schema}].[{table}]"
        ).scalar_one()
        if int(observed) != expected:
            raise RuntimeError(f"Frozen row-count anchor mismatch for {table}")


def find_completed_copy_only_backup(conn, database: str, path: Path):
    return conn.execute(
        text("""
            SELECT TOP (1)
                bs.backup_finish_date,
                bs.compressed_backup_size,
                bs.is_damaged,
                bs.has_backup_checksums
            FROM msdb.dbo.backupset AS bs
            INNER JOIN msdb.dbo.backupmediafamily AS bm
                ON bs.media_set_id = bm.media_set_id
            WHERE bs.database_name = :db
              AND bm.physical_device_name = :path
              AND bs.type = 'D'
              AND bs.is_copy_only = 1
              AND bs.backup_finish_date IS NOT NULL
            ORDER BY bs.backup_finish_date DESC
        """),
        {"db": database, "path": str(path)},
    ).first()


def execute_backup(config_path: str) -> None:
    cfg = load_etl_config(config_path)
    db = str(cfg.raw["sqlserver"]["database"])
    schema = str(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    if db != FROZEN_DATABASE:
        raise RuntimeError("Backup blocked: database differs from frozen publication target")
    if str(cfg.raw["etl"]["cdm_version"]) != "5.4.2":
        raise RuntimeError("Backup blocked: expected frozen OMOP CDM v5.4.2")
    if not cfg.sql_password:
        raise RuntimeError("Backup blocked: SQL Server password environment variable missing")

    if not BACKUP_DIRECTORY.is_dir():
        raise RuntimeError("Backup directory does not exist; configure as protected mssql directory")
    free_gib = shutil.disk_usage("/var/opt/mssql").free / (1024 ** 3)
    if free_gib < MIN_FREE_GIB:
        raise RuntimeError("Backup blocked: insufficient free disk space")
    print(f"Available disk space: {free_gib:.1f} GiB", flush=True)

    engine = make_engine(cfg, database=db)
    state = "frozen target preflight"
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            check_frozen_anchor(conn, db, schema)
            print("Frozen publication row-count anchors: PASS", flush=True)
            driver_conn = conn.connection.driver_connection
            if not driver_conn.autocommit:
                raise RuntimeError("Database connection is not in AUTOCOMMIT mode")

            path = build_backup_path()
            print("Backup destination:", path, flush=True)
            print(
                "BACKUP STARTED; keep this process connected until verification completes.",
                flush=True,
            )
            state = "BACKUP DATABASE"
            drain_dbapi_results(
                driver_conn,
                f"BACKUP DATABASE [{db}] TO DISK = N'{path}' "
                "WITH COPY_ONLY, COMPRESSION, CHECKSUM, NOINIT, STATS = 10",
            )

            # The cursor finishing is essential but not sufficient evidence.
            state = "SQL Server msdb backup-history verification"
            metadata = find_completed_copy_only_backup(conn, db, path)
            if metadata is None:
                raise RuntimeError("No completed matching copy-only backup in msdb")
            finish_date, compressed_size, is_damaged, has_checksum = metadata
            if finish_date is None or int(is_damaged) != 0 or int(has_checksum) != 1:
                raise RuntimeError("SQL Server backup history reports damaged/incomplete media")
            if compressed_size is None or int(compressed_size) <= 0:
                raise RuntimeError("SQL Server reported empty backup media")
            print(
                "SQL Server backup history: PASS; "
                f"compressed size: {int(compressed_size)/(1024**3):.2f} GiB",
                flush=True,
            )

            state = "RESTORE VERIFYONLY"
            print("Verifying backup with CHECKSUM and STOP_ON_ERROR...", flush=True)
            drain_dbapi_results(
                driver_conn,
                f"RESTORE VERIFYONLY FROM DISK = N'{path}' "
                "WITH CHECKSUM, STOP_ON_ERROR",
            )
            print("RESTORE VERIFYONLY: PASS", flush=True)

            state = "post-backup frozen target anchor check"
            check_frozen_anchor(conn, db, schema)
            print("Frozen publication row-count anchors remain unchanged: PASS", flush=True)
            print("BACKUP AND VERIFICATION: PASS", flush=True)
            print("Protected backup location:", path, flush=True)
    except Exception as exc:
        # DB-API exceptions may include a password-bearing connection string
        # or provider parameters. Never expose raw provider exception content.
        print(f"BACKUP FAILED during: {state}", file=sys.stderr)
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        else:
            print("Database error details suppressed to protect credentials.", file=sys.stderr)
        print("Do NOT provision the QC login yet.", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy-only backup with full result drain and VERIFYONLY")
    parser.add_argument("--config", default="config/etl_A.yaml")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform a BACKUP and RESTORE VERIFYONLY after confirming protected storage",
    )
    args = parser.parse_args()
    print("Frozen target:", FROZEN_DATABASE)
    print("Backup directory:", BACKUP_DIRECTORY)
    if not args.execute:
        print("PREVIEW ONLY. No SQL connection or backup was made.")
        print(
            "Ensure the directory is writable by mssql and the filesystem "
            "is institution-approved and encrypted before using --execute."
        )
        return
    execute_backup(args.config)


if __name__ == "__main__":
    main()
