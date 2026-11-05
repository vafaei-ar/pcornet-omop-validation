#!/usr/bin/env python3
"""Independently reconcile saved Achilles 210 with the frozen SQL Server.

Read-only: confirms the original count predicate against live OMOP data
using the pre-existing, restricted QC principal. Does not rerun Achilles,
materialize cohorts, write files, or display small-cell counts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_postfreeze_achilles_exception_counts import (  # noqa: E402
    EXCEPTION_SQL,
    THRESHOLD,
    verify_missing_ids,
)
from audit_postfreeze_achilles_210_cohort_overlap import (  # noqa: E402
    COLUMNS,
    EXPECTED_OMOP_D0,
    EXPECTED_SOURCE_D0,
    OUTPUT,
    validate_private_metadata,
)
from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    FROZEN_DATABASE,
    QC_DATABASE,
    inspect_frozen,
    make_restricted_odbc_string,
    single_scalar,
    verified_server_config,
    verify_manifest,
)


def verify_previous_overlap_artifact(expected: int) -> None:
    """Validate prior cohort-shortcut provenance without printing its values."""
    directory = OUTPUT.parent
    if directory.is_symlink() or not directory.is_dir():
        raise AuditBlocked("Private overlap directory missing or unsafe")
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise AuditBlocked("Private overlap directory permissions unsafe")
    try:
        metadata = OUTPUT.lstat()
    except OSError:
        raise AuditBlocked("Previous private overlap result not found") from None
    if (not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= 16384):
        raise AuditBlocked("Previous private overlap result metadata unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(OUTPUT, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            checked = os.fstat(stream.fileno())
            if ((metadata.st_dev, metadata.st_ino)
                    != (checked.st_dev, checked.st_ino)):
                raise AuditBlocked("Previous private overlap result changed")
            report = json.load(stream)
    except AuditBlocked:
        raise
    except (OSError, ValueError, UnicodeError, TypeError):
        raise AuditBlocked("Previous private overlap result malformed") from None
    if (not isinstance(report, dict)
            or report.get("status")
                != "private_postfreeze_achilles_210_cohort_overlap"
            or report.get("frozen_etl_sha")
                != "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
            or report.get("study_definition") != "stage-c-stroke-d0-v1"
            or type(report.get("achilles_210_exception_count")) is not int
            or report["achilles_210_exception_count"] != expected):
        raise AuditBlocked("Previous private overlap provenance mismatch")
    performed = report.get("cohort_sql_performed")
    detail = report.get("cohort_overlap")
    if expected == 0:
        if performed is not False or detail is not None:
            raise AuditBlocked("Zero-exception shortcut metadata inconsistent")
    else:
        if performed is not True or not isinstance(detail, dict):
            raise AuditBlocked("Prior positive-count cohort SQL not documented")
        if set(detail) != set(COLUMNS):
            raise AuditBlocked("Incomplete prior cohort overlap metrics")
        if any(type(v) is not int or v < 0 for v in detail.values()):
            raise AuditBlocked("Malformed prior cohort overlap metrics")
        if (detail["source_d0_patients"] != EXPECTED_SOURCE_D0
                or detail["omop_lineage_d0_patients"] != EXPECTED_OMOP_D0
                or detail["exception_visits"] != expected):
            raise AuditBlocked("Previous overlap anchor mismatch")
        if any(detail[name] > expected for name in COLUMNS[3:]):
            raise AuditBlocked("Previous overlap count exceeds exception total")
        if (detail["exception_index_visits_on_omop_d0"]
                > detail["exception_visits_on_omop_d0_patients"]
                or detail["exception_omop_d0_patients"]
                > detail["exception_visits_on_omop_d0_patients"]
                or detail["exception_acute_visits_30d_on_omop_d0"]
                > detail["exception_acute_visits_90d_on_omop_d0"]):
            raise AuditBlocked("Prior overlap metrics inconsistent")


def confirm_against_frozen(config_path: str) -> None:
    verify_manifest()
    expected = validate_private_metadata()
    verify_previous_overlap_artifact(expected)
    secret = os.getenv("OHDSI_QC_PASSWORD", "")
    if not secret:
        raise AuditBlocked("OHDSI_QC_PASSWORD missing from current shell")
    config = verified_server_config(config_path)
    import pyodbc

    qc = pyodbc.connect(
        make_restricted_odbc_string(config, QC_DATABASE, secret),
        autocommit=True, timeout=20,
    )
    try:
        verify_missing_ids(qc)
    finally:
        qc.close()

    frozen = pyodbc.connect(
        make_restricted_odbc_string(config, FROZEN_DATABASE, secret),
        autocommit=True, timeout=20,
    )
    try:
        inspect_frozen(frozen)
        actual = single_scalar(frozen, EXCEPTION_SQL[210])
    finally:
        frozen.close()

    if actual > THRESHOLD or actual != expected:
        raise AuditBlocked(
            "Live frozen analysis-210 count does not match prior "
            "suppressed-result evidence; investigate locally"
        )

    print("ACHILLES 210 LIVE FROZEN COUNT RECONCILIATION: PASS")
    print("Restricted read-only SQL count matches the protected prior evidence.")
    print("Existing private overlap report is consistent with that evidence.")
    print("No patient rows or small-cell counts exported; no files modified.")
    print("This verifies the exception count, not an independent cohort rerun.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only live verification of protected Achilles 210 result"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    args = parser.parse_args()
    try:
        confirm_against_frozen(args.config)
    except AuditBlocked as exc:
        print("ACHILLES 210 LIVE VERIFICATION BLOCKED:", str(exc),
              file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # SQL provider exceptions can contain sensitive connection information.
        print("ACHILLES 210 LIVE VERIFICATION BLOCKED: provider error suppressed",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
