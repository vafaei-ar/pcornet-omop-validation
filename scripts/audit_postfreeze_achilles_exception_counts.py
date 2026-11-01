#!/usr/bin/env python3
"""Read-only targeted Achilles exception-count triage for the frozen OMOP CDM.

Exactly reproduce the count predicates in OHDSI Achilles v1.7.2 analyses
207, 209, 210 and 814. Check the saved Achilles coverage before reading the
frozen CDM. The only SQL account allowed is the already provisioned,
restricted ohdsi_postfreeze_qc account.

Never print exception counts. Write exact counts to a new, private 0600 file
under ignored results/, with a 0700 parent directory. No patient IDs,
strata, concepts, individual rows, or clinical values leave SQL Server.
This is NOT an Achilles rerun and NOT a DQD pass.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    FROZEN_DATABASE,
    QC_DATABASE,
    ROOT,
    inspect_frozen,
    make_restricted_odbc_string,
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

# OHDSI Achilles 1.7.2 SQL Server:
# https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/
# These SELECTs retain the original COUNT_BIG argument and predicates,
# removing only the SELECT ... INTO scratch-table output.
EXCEPTION_SQL = {
    207: """
        SELECT COUNT_BIG(vo1.PERSON_ID)
        FROM [dbo].[visit_occurrence] AS vo1
        LEFT JOIN [dbo].[person] AS p1 ON p1.person_id = vo1.person_id
        WHERE p1.person_id IS NULL
    """,
    209: """
        SELECT COUNT_BIG(vo1.PERSON_ID)
        FROM [dbo].[visit_occurrence] AS vo1
        LEFT JOIN [dbo].[care_site] AS cs1
            ON vo1.care_site_id = cs1.care_site_id
        WHERE vo1.care_site_id IS NOT NULL AND cs1.care_site_id IS NULL
    """,
    210: """
        SELECT COUNT_BIG(*)
        FROM [dbo].[visit_occurrence] AS vo
        LEFT JOIN [dbo].[observation_period] AS op
            ON vo.person_id = op.person_id
            AND vo.visit_start_date >= op.observation_period_start_date
            AND vo.visit_start_date <= op.observation_period_end_date
        WHERE op.person_id IS NULL
    """,
    814: """
        SELECT COUNT_BIG(o1.PERSON_ID)
        FROM [dbo].[observation] AS o1
        WHERE o1.value_as_number IS NULL
          AND o1.value_as_string IS NULL
          AND o1.value_as_concept_id IS NULL
    """,
}
CATEGORIES = {
    207: "Visit Occurrence",
    209: "Visit Occurrence",
    210: "Observation Period",
    814: "Observation",
}
THRESHOLD = 10
PRIVATE_DIR = ROOT / "results" / "postfreeze_standard_qc" / "private_achilles_exception_counts"
PRIVATE_OUTPUT = PRIVATE_DIR / "four_analysis_exact_counts.json"


def verify_missing_ids(conn) -> None:
    """Require the known completed Achilles coverage before targeted counts."""
    verify_identity(conn, QC_DATABASE)
    tables = discover_achilles_tables(conn)
    cursor = conn.cursor()
    try:
        cursor.execute(coverage_sql(tables))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    records = parse_definition_rows(rows)
    summarize_coverage(records)  # Frozen snapshot: 194 default, 140 with data.
    indexed = {record["id"]: record for record in records}
    for analysis_id, expected_category in CATEGORIES.items():
        item = indexed.get(analysis_id)
        if (item is None or item["default"] != 1
                or item["category"] != expected_category
                or item["has_data"] != 0
                or item["has_performance"] != 1):
            raise AuditBlocked(
                "Saved Achilles missing-result status has changed: "
                f"analysis {analysis_id}"
            )


def read_exception_counts(conn) -> dict[int, int]:
    """Read only four official exception counts; never fetch matching records."""
    verify_identity(conn, FROZEN_DATABASE)
    counts = {}
    for analysis_id, query in EXCEPTION_SQL.items():
        if not query.lstrip().upper().startswith("SELECT COUNT_BIG("):
            raise AuditBlocked("Non-SELECT exception diagnostic rejected")
        count = single_scalar(conn, query)
        # A count above ten contradicts the suppressed result interpretation,
        # assuming the persisted Achilles output has not been replaced.
        if count > THRESHOLD:
            raise AuditBlocked(
                f"Analysis {analysis_id} exceeds saved suppression threshold; "
                "do not export counts, investigate possible CDM/QC output drift"
            )
        counts[analysis_id] = count
    return counts


def _private_directory(path: Path) -> None:
    """Reject symlinked output components and group/world-accessible private dir."""
    for ancestor in (ROOT / "results", ROOT / "results" / "postfreeze_standard_qc", path):
        if ancestor.is_symlink():
            raise AuditBlocked("Private result path contains an unsafe symlink")
    if not path.is_relative_to(ROOT / "results" / "postfreeze_standard_qc"):
        raise AuditBlocked("Private output must remain in ignored local results")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise AuditBlocked("Private result directory must have mode 0700")


def save_private_counts(counts: dict[int, int], path: Path = PRIVATE_OUTPUT) -> None:
    """Create once, fail if present, never overwrite or print exact counts."""
    if set(counts) != set(EXCEPTION_SQL):
        raise AuditBlocked("Incomplete four-ID exception-count response")
    if any(type(n) is not int or not 0 <= n <= THRESHOLD
           for n in counts.values()):
        raise AuditBlocked("Invalid or out-of-range exception count")
    if path.parent != PRIVATE_DIR:
        raise AuditBlocked("Unapproved private output directory")
    _private_directory(path.parent)
    payload = {
        "status": "private_read_only_achilles_exception_triage",
        "achilles_version": "1.7.2",
        "cdm_version": "5.4.2",
        "small_cell_threshold": THRESHOLD,
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "interpretation": (
            "All four original Achilles count predicates reproduced with SELECT. "
            "Zero vs 1-10 is private, not publication-cleared. "
            "No cohort-impact inference has been made."
        ),
        "analyses": {
            str(analysis_id): {
                "exception_count": counts[analysis_id],
                "classification": (
                    "ZERO" if counts[analysis_id] == 0 else
                    "SUPPRESSED_NONZERO"
                ),
            }
            for analysis_id in sorted(counts)
        },
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise AuditBlocked("Private result already exists; refusing overwrite") from None
    except OSError:
        raise AuditBlocked("Could not create protected private result file") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise AuditBlocked("Could not safely write private result file") from None


def verify_existing_private_output(path: Path | None = None) -> None:
    """Check an existing local file without SQL, changes, or count disclosure.

    This establishes schema/permissions only, NOT that the saved values came
    from a particular database execution or remain current.
    """
    path = PRIVATE_OUTPUT if path is None else path
    if path != PRIVATE_OUTPUT or path.parent != PRIVATE_DIR:
        raise AuditBlocked("Unapproved private output path")
    for parent in (ROOT / "results", ROOT / "results" / "postfreeze_standard_qc",
                   PRIVATE_DIR):
        if parent.is_symlink():
            raise AuditBlocked("Private result path contains an unsafe symlink")
    if not PRIVATE_DIR.is_dir() or (
        stat.S_IMODE(PRIVATE_DIR.stat().st_mode) != 0o700
    ):
        raise AuditBlocked("Private result directory must have mode 0700")
    try:
        file_stat = path.lstat()
    except OSError:
        raise AuditBlocked("Expected private result file not found") from None
    if (not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
            or file_stat.st_uid != os.getuid()
            or not 0 < file_stat.st_size <= 16384):
        raise AuditBlocked("Unsafe private result file type, size or permissions")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            data_stat = os.fstat(stream.fileno())
            if (not stat.S_ISREG(data_stat.st_mode)
                    or (data_stat.st_dev, data_stat.st_ino)
                    != (file_stat.st_dev, file_stat.st_ino)):
                raise AuditBlocked("Private result file changed during read")
            payload = json.load(stream)
    except AuditBlocked:
        raise
    except (OSError, ValueError, UnicodeError, TypeError):
        raise AuditBlocked("Unreadable or malformed private result file") from None

    expected = {
        "status": "private_read_only_achilles_exception_triage",
        "achilles_version": "1.7.2",
        "cdm_version": "5.4.2",
        "small_cell_threshold": THRESHOLD,
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
    }
    if not isinstance(payload, dict) or any(
        type(payload.get(k)) is not type(v) or payload.get(k) != v
        for k, v in expected.items()
    ):
        raise AuditBlocked("Private result provenance fields mismatch")
    try:
        recorded = datetime.fromisoformat(payload["recorded_at_utc"])
        if recorded.tzinfo is None or recorded.utcoffset() is None:
            raise ValueError("Timestamp must be timezone-aware")
    except (KeyError, ValueError, TypeError, OverflowError):
        raise AuditBlocked("Private result has invalid recorded timestamp") from None
    if not isinstance(payload.get("interpretation"), str) or not payload["interpretation"]:
        raise AuditBlocked("Private result has invalid interpretation metadata")
    entries = payload.get("analyses")
    if not isinstance(entries, dict) or set(entries) != {
        str(k) for k in EXCEPTION_SQL
    }:
        raise AuditBlocked("Private result has incomplete analysis IDs")
    for entry in entries.values():
        if (not isinstance(entry, dict)
                or set(entry) != {"exception_count", "classification"}):
            raise AuditBlocked("Private result has malformed analysis entries")
        count = entry["exception_count"]
        if type(count) is not int or not 0 <= count <= THRESHOLD:
            raise AuditBlocked("Private result has invalid analysis count")
        if entry["classification"] != (
            "ZERO" if count == 0 else "SUPPRESSED_NONZERO"
        ):
            raise AuditBlocked("Private result classification mismatch")

    print("EXISTING PRIVATE ACHILLES FILE: STRUCTURE/PERMISSIONS PASS")
    print("Only the local file was checked; no SQL connection or rerun occurred.")
    print("This does not independently authenticate saved counts or prove cohort impact.")
    print("Do NOT upload, paste, commit or publish the private file.")


def inspect(config_path: str) -> None:
    verify_manifest()
    config = verified_server_config(config_path)
    password = os.getenv("OHDSI_QC_PASSWORD", "")
    if not password:
        raise AuditBlocked("OHDSI_QC_PASSWORD missing from current shell")
    if PRIVATE_OUTPUT.exists() or PRIVATE_OUTPUT.is_symlink():
        raise AuditBlocked("Private result already exists; refusing overwrite")

    import pyodbc

    qc = pyodbc.connect(
        make_restricted_odbc_string(config, QC_DATABASE, password),
        autocommit=True, timeout=20,
    )
    try:
        verify_missing_ids(qc)
    finally:
        qc.close()

    frozen = pyodbc.connect(
        make_restricted_odbc_string(config, FROZEN_DATABASE, password),
        autocommit=True, timeout=20,
    )
    try:
        inspect_frozen(frozen)  # All reference anchors and empty CDM_SOURCE.
        counts = read_exception_counts(frozen)
    finally:
        frozen.close()

    save_private_counts(counts)
    print("FOUR-ANALYSIS READ-ONLY TRIAGE: PASS")
    print("Exact exception counts saved only in a private local 0600 file:")
    print("  results/postfreeze_standard_qc/private_achilles_exception_counts/"
          "four_analysis_exact_counts.json")
    print("DO NOT upload, commit, paste or publish this file or its small counts.")
    print("No row-level records retrieved; no frozen data or QC tables modified.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SELECT-only, private exact-count check of suppressed Achilles IDs"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    parser.add_argument(
        "--verify-existing", action="store_true",
        help="Validate existing private JSON format/permissions without SQL or count disclosure",
    )
    args = parser.parse_args()
    try:
        if args.verify_existing:
            verify_existing_private_output()
        else:
            inspect(args.config)
    except AuditBlocked as error:
        print("FOUR-ANALYSIS TRIAGE BLOCKED:", str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # SQL/driver exceptions may include credentials or clinical SQL details.
        print("FOUR-ANALYSIS TRIAGE BLOCKED: provider error suppressed",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
