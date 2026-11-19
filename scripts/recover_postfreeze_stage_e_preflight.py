#!/usr/bin/env python3
"""Guarded recovery of missing historical Stage E preflight metadata.

Reruns ORIGINAL outcome-free table/column checks with dedicated restricted QC
login on the frozen database. Never performs study outcomes, fits, ETL changes,
or an administrator SQL login. Writes a new provenance-labeled local JSON
only when the expected artifact is absent.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from urllib.parse import quote_plus

from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_postfreeze_achilles import (  # noqa: E402
    AuditBlocked,
    FROZEN_DATABASE,
    ROOT,
    inspect_frozen,
    make_restricted_odbc_string,
    verified_server_config,
)
from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.study import (
    stage_e_statistical_model_preflight as original,
)

EXPECTED = (
    ROOT / "results/publication_analysis/stage_e_statistical_model/"
    "stage_e_statistical_model_preflight.json"
)
FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def destination_for(config_path: str) -> Path:
    config = load_etl_config(config_path)
    candidate = (
        config.audit_dir.parent / "publication_analysis"
        / "stage_e_statistical_model" / EXPECTED.name
    )
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if candidate != EXPECTED:
        raise AuditBlocked("Configured Stage E preflight location differs from locked path")
    for parent in (ROOT / "results", ROOT / "results/publication_analysis",
                   EXPECTED.parent):
        if parent.is_symlink():
            raise AuditBlocked("Stage E preflight location contains a symlink")
    if EXPECTED.exists() or EXPECTED.is_symlink():
        raise AuditBlocked("Stage E preflight already exists; refusing overwrite")
    return EXPECTED


def validate_regenerated(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise AuditBlocked("Original Stage E preflight returned invalid metadata")
    if (payload.get("status") != "stage_e_statistical_model_preflight_ready"
            or payload.get("frozen_etl_sha") != FROZEN_SHA
            or payload.get("outcome_query_performed") is not False
            or payload.get("model_fit_performed") is not False
            or payload.get("analysis_worktree_clean") is not True
            or not isinstance(payload.get("recorded_at_utc"), str)):
        raise AuditBlocked("Original Stage E preflight did not pass required guards")
    from hashlib import sha256
    for field, path in (
        ("study_definition_sha256", original.STUDY_PATH),
        ("inherited_d0_definition_sha256", original.D0_PATH),
        ("inherited_stage_d_definition_sha256", original.STAGE_D_PATH),
    ):
        if not path.is_file() or payload.get(field) != sha256(path.read_bytes()).hexdigest():
            raise AuditBlocked("Original Stage E study-definition hash mismatch")
    checks = payload.get("feature_checks")
    if not isinstance(checks, dict) or not checks or not all(
        value is True for value in checks.values()
    ):
        raise AuditBlocked("Original Stage E required-feature checks are incomplete")
    result = dict(payload)
    result["postfreeze_recovery_provenance"] = {
        "role": "regenerated_outcome_free_stage_e_metadata",
        "historical_original_file": "missing_from_local_results",
        "original_run_date_preserved": False,
        "frozen_database_modified": False,
        "restricted_qc_login": True,
        "date": payload["recorded_at_utc"],
    }
    return result


def recover(config_path: str) -> None:
    destination = destination_for(config_path)
    server = verified_server_config(config_path)
    password = os.getenv("OHDSI_QC_PASSWORD", "")
    if not password:
        raise AuditBlocked("OHDSI_QC_PASSWORD missing in current terminal")

    # Reproduce the frozen publication anchors before the metadata-only run.
    import pyodbc
    conn_str = make_restricted_odbc_string(server, FROZEN_DATABASE, password)
    conn = pyodbc.connect(conn_str, autocommit=True, timeout=20)
    try:
        inspect_frozen(conn)
    finally:
        conn.close()

    # The unmodified original preflight uses the SQLAlchemy engine supplied
    # here. The restricted login is used instead of the ETL administrator.
    # The original module's engine binding is restored, even on exceptions.
    def restricted_engine(_cfg):
        return create_engine(
            "mssql+pyodbc:///?odbc_connect=" + quote_plus(conn_str),
            future=True,
            pool_pre_ping=True,
        )

    staging_parent = ROOT / "results/postfreeze_standard_qc"
    staging_parent.mkdir(parents=True, exist_ok=True)
    if staging_parent.is_symlink():
        raise AuditBlocked("Unsafe private staging location")

    with tempfile.TemporaryDirectory(prefix="stage_e_preflight_recovery_",
                                     dir=staging_parent) as staging:
        old_factory = original.make_engine
        original.make_engine = restricted_engine
        try:
            payload = original.run(config_path, output_dir=staging)
        finally:
            original.make_engine = old_factory
        safe = validate_regenerated(payload)

        # Never overwrite an existing artifact, including a late-created
        # artifact. Output is aggregate metadata, not any patient-level data.
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(destination, flags, 0o600)
        except OSError:
            raise AuditBlocked("Cannot create Stage E report without overwrite") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(safe, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise AuditBlocked("Failed to save regenerated Stage E preflight") from None

    if stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise AuditBlocked("Regenerated Stage E file has unsafe permissions")
    print("STAGE E OUTCOME-FREE METADATA RECOVERY: PASS")
    print("Restricted frozen publication row-count anchors: PASS")
    print("Regenerated metadata is labeled, not the original historical file.")
    print("No clinical table writes, outcome queries or models executed.")
    print("Local ignored file: results/publication_analysis/stage_e_statistical_model/"
          "stage_e_statistical_model_preflight.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recreate missing Stage E outcome-free metadata with QC login"
    )
    parser.add_argument("--config", default="config/etl_A.yaml")
    args = parser.parse_args()
    try:
        recover(args.config)
    except AuditBlocked as exc:
        print("STAGE E METADATA RECOVERY BLOCKED:", str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Driver errors may contain confidential connection details.
        print("STAGE E METADATA RECOVERY BLOCKED: provider error suppressed",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
