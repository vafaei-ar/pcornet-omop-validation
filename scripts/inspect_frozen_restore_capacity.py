#!/usr/bin/env python3
"""Read-only SQL Server backup FILELISTONLY capacity and media provenance check.

Run only on the authorized institutional SQL Server instance. Reads msdb and
RESTORE FILELISTONLY for the one already-verified COPY_ONLY backup. Never
executes RESTORE DATABASE, BACKUP, SQL DDL/DML, or filesystem writes. The
existing ETL administrator login may be needed to access backup metadata.
Never print DSNs, passwords, file paths or raw provider exceptions.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import re
import shutil
import sys

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.postfreeze_restricted_qc import (
    FROZEN_DATABASE,
    verified_server_config,
)

BACKUP_DIR = Path("/var/opt/mssql/backup")
GIB = 1024 ** 3
BACKUP_RE = re.compile(
    r"/var/opt/mssql/backup/OMOP_VALIDATED_A_copyonly_"
    r"[0-9]{8}T[0-9]{6}Z_[0-9a-f]{10}\.bak\Z"
)


class CapacityBlocked(RuntimeError):
    pass


def verified_backup_history(rows: list) -> tuple[str, int, int]:
    """Require exactly one known healthy local COPY_ONLY backup set."""
    if len(rows) != 1:
        raise CapacityBlocked("Ambiguous or missing protected backup media history")
    row = rows[0]
    filename = str(row["physical_device_name"])
    position = row["position"]
    size = row["compressed_backup_size"]
    if (not BACKUP_RE.fullmatch(filename)
            or type(position) is not int or not (1 <= position <= 32767)
            or type(size) is not int or size <= 0):
        raise CapacityBlocked("Unexpected protected backup media metadata")
    return filename, position, size


def integral_bytes(value: object) -> int:
    """SQL numeric(20,0) commonly arrives from pyodbc as Decimal."""
    if isinstance(value, bool):
        raise CapacityBlocked("Invalid backup size type")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        parsed = int(value)
    else:
        raise CapacityBlocked("Nonintegral backup file allocation")
    if parsed < 0:
        raise CapacityBlocked("Negative backup file allocation")
    return parsed


def summarize_filelist(rows: list) -> dict[str, int]:
    """Use the backup's own logical allocation sizes, never the .bak size."""
    if not rows:
        raise CapacityBlocked("Empty RESTORE FILELISTONLY")
    data_size = 0
    log_size = 0
    ancillary = 0
    for raw in rows:
        row = {str(k).casefold(): v for k, v in dict(raw).items()}
        file_type = str(row.get("type", "")).upper()
        length = integral_bytes(row.get("size"))
        present = row.get("ispresent")
        if (file_type not in {"D", "L", "S", "F"}
                or present not in (1, True)):
            raise CapacityBlocked("Unexpected or incomplete backup filelist")
        if file_type == "D":
            data_size += length
        elif file_type == "L":
            log_size += length
        else:
            ancillary += length
    if data_size == 0 or log_size == 0:
        raise CapacityBlocked("Backup file list missing data or log files")
    return {
        "file_count": len(rows),
        "data_bytes": data_size,
        "log_bytes": log_size,
        "other_bytes": ancillary,
        "total_bytes": data_size + log_size + ancillary,
    }


def inspect(config_path: str) -> None:
    # This requires the prior approved ETL administrator connection to read
    # backup-set metadata, but performs read-only SQL and never writes the CDM.
    verified_server_config(config_path)
    cfg = load_etl_config(config_path)
    if not cfg.sql_password:
        raise CapacityBlocked("SQL admin password environment variable unavailable")

    engine = make_engine(cfg, database=FROZEN_DATABASE)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            db_name = conn.execute(text("SELECT DB_NAME()")).scalar_one()
            if str(db_name).casefold() != FROZEN_DATABASE.casefold():
                raise CapacityBlocked("Unexpected SQL database identity")
            records = conn.execute(text("""
                SELECT bs.position, bm.physical_device_name,
                       bs.compressed_backup_size
                  FROM msdb.dbo.backupset AS bs
                  JOIN msdb.dbo.backupmediafamily AS bm
                    ON bs.media_set_id = bm.media_set_id
                 WHERE bs.database_name = :database_name
                   AND bs.type = 'D'
                   AND bs.is_copy_only = 1
                   AND bs.is_damaged = 0
                   AND bs.has_backup_checksums = 1
                   AND bs.backup_finish_date IS NOT NULL
                   AND bm.physical_device_name LIKE :prefix
            """), {
                "database_name": FROZEN_DATABASE,
                "prefix": str(BACKUP_DIR / (FROZEN_DATABASE + "_copyonly_%")),
            }).mappings().all()
            filename, position, size = verified_backup_history(records)
            # SQL identifier/string literals are safe here because the complete
            # path and integral media position were independently validated.
            sql = (
                "RESTORE FILELISTONLY FROM DISK = N'"
                + filename + "' WITH FILE = " + str(position)
            )
            raw = conn.exec_driver_sql(sql).mappings().all()
            allocation = summarize_filelist(raw)

        available_bytes = shutil.disk_usage("/var/opt/mssql").free
        print("FROZEN BACKUP FILELISTONLY CAPACITY: PASS")
        print("Source database identity and checksum backup history: PASS")
        print("Logical database files:", allocation["file_count"])
        print(f"Data allocation: {allocation['data_bytes']/GIB:.2f} GiB")
        print(f"Log allocation: {allocation['log_bytes']/GIB:.2f} GiB")
        print(f"Other allocation: {allocation['other_bytes']/GIB:.2f} GiB")
        print(f"Total logical allocation: {allocation['total_bytes']/GIB:.2f} GiB")
        print(f"Compressed existing backup: {size/GIB:.2f} GiB")
        print(f"Current local free space: {available_bytes/GIB:.2f} GiB")
        print("These are capacity estimates, not a successful isolated restore.")
        print("Do not restore, transfer or amend frozen CDM without separate authorization.")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only backup filelist capacity inspection")
    parser.add_argument("--config", default="config/etl_A.yaml")
    args = parser.parse_args()
    try:
        inspect(args.config)
    except Exception:
        # SQL driver errors may include credentials, internal paths or metadata.
        print("FROZEN BACKUP FILELISTONLY CAPACITY: BLOCKED", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
