#!/usr/bin/env python3
"""Opt-in one-row CDM_SOURCE metadata amendment in the frozen OMOP database.

Only the explicitly named dbo.cdm_source table may be inserted into. Read
locks/counts, identity, vocabulary, concept, and verified provenance first.
Default is no-SQL preview; --preflight runs SELECT-only SQL; --execute requires
a separate exact confirmation and a single protected SQL transaction.
Never prints patient data, metadata values, DBAPI errors, or credentials.
"""
from __future__ import annotations

import argparse
from datetime import date
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.postfreeze_restricted_qc import (
    FROZEN_DATABASE, verified_server_config,
)

_spec = importlib.util.spec_from_file_location(
    "_frozen_cdm_manifest_validator",
    Path(__file__).with_name("validate_restored_cdm_source_manifest.py"),
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Offline CDM_SOURCE manifest validation unavailable")
manifest_validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manifest_validator)

CONFIRM = "INSERT_ONE_CDM_SOURCE_METADATA_ROW_IN_OMOP_VALIDATED_A"
ANCHORS = {
    "person": 27089,
    "visit_occurrence": 1510957,
    "condition_occurrence": 7315572,
}
FIELDS = tuple(manifest_validator.FIELD_LIMITS)
PHASES = frozenset({
    "CONFIG", "MANIFEST", "CREDENTIAL", "CONNECT", "IDENTITY",
    "CLINICAL_ANCHORS", "CDM_SOURCE_EMPTY", "METADATA_SCHEMA",
    "VOCABULARY", "CDM_VERSION_CONCEPT", "INSERT_PERMISSION",
    "INSERT", "POST_VERIFY",
})


class AmendmentBlocked(RuntimeError):
    pass


def safe_phase(phase: str) -> str:
    return phase if phase in PHASES else "UNCLASSIFIED"


def load_private_manifest(path: str) -> dict:
    p = Path(path)
    manifest_validator.validate_private_path(p)
    with p.open("r", encoding="utf-8") as stream:
        record = json.load(stream)
    manifest_validator.validate(record)
    return record["cdm_source"]


def safe_connection_config(config_path: str):
    verified_server_config(config_path)
    cfg = load_etl_config(config_path)
    if str(cfg.raw["sqlserver"].get("user", "")).strip() == "":
        raise AmendmentBlocked("CONFIG")
    return cfg


def credential_ready(cfg, *, interactive: bool) -> bool:
    name = cfg.raw["sqlserver"].get("password_env")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise AmendmentBlocked("CREDENTIAL")
    if cfg.sql_password:
        return True
    if not interactive or not sys.stdin.isatty():
        raise AmendmentBlocked("CREDENTIAL")
    secret = getpass.getpass("Authorized existing ETL SQL password (hidden): ")
    if not secret:
        raise AmendmentBlocked("CREDENTIAL")
    os.environ[name] = secret
    del secret
    return True


def assert_identity(conn, cfg) -> None:
    row = conn.exec_driver_sql("SELECT DB_NAME(), ORIGINAL_LOGIN()").one()
    expected_user = str(cfg.raw["sqlserver"]["user"])
    if (len(row) != 2
            or str(row[0]).casefold() != FROZEN_DATABASE.casefold()
            or str(row[1]).casefold() != expected_user.casefold()):
        raise AmendmentBlocked("IDENTITY")


def assert_clinical_anchors(conn) -> None:
    for table, expected in ANCHORS.items():
        n = conn.exec_driver_sql(
            f"SELECT COUNT_BIG(*) FROM [dbo].[{table}]"
        ).scalar_one()
        if type(n) is not int or n != expected:
            raise AmendmentBlocked("CLINICAL_ANCHORS")


def assert_empty(conn) -> None:
    n = conn.exec_driver_sql(
        "SELECT COUNT_BIG(*) FROM [dbo].[cdm_source]"
    ).scalar_one()
    if n != 0:
        raise AmendmentBlocked("CDM_SOURCE_EMPTY")


def assert_metadata_schema(conn) -> None:
    # Reject a drifted table or unexpected SQL columns, including novel
    # non-null columns not present in the official 5.4 manifest.
    rows = conn.exec_driver_sql("""
        SELECT COLUMN_NAME
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'cdm_source'
    """).all()
    if set(str(x[0]).casefold() for x in rows) != set(FIELDS) or len(rows) != len(FIELDS):
        raise AmendmentBlocked("METADATA_SCHEMA")


def assert_vocabulary(conn, row: dict) -> None:
    found = conn.exec_driver_sql(
        "SELECT vocabulary_version FROM [dbo].[vocabulary] "
        "WHERE vocabulary_id = 'None'"
    ).all()
    if (len(found) != 1 or found[0][0] is None
            or str(found[0][0]) != row["vocabulary_version"]):
        raise AmendmentBlocked("VOCABULARY")


def assert_concept(conn, row: dict) -> None:
    found = conn.execute(text("""
        SELECT vocabulary_id, concept_class_id, domain_id
          FROM [dbo].[concept]
         WHERE concept_id = :id
    """), {"id": row["cdm_version_concept_id"]}).all()
    if (len(found) != 1 or str(found[0][0]) != "CDM"
            or str(found[0][1]) != "CDM"
            or str(found[0][2]) != "Metadata"):
        raise AmendmentBlocked("CDM_VERSION_CONCEPT")


def assert_insert_right(conn) -> None:
    right = conn.exec_driver_sql(
        "SELECT HAS_PERMS_BY_NAME('dbo.cdm_source','OBJECT','INSERT')"
    ).scalar_one()
    if right != 1:
        raise AmendmentBlocked("INSERT_PERMISSION")


def perform(config_path: str, manifest_path: str, *, execute: bool) -> None:
    phase = "CONFIG"
    engine = None
    try:
        cfg = safe_connection_config(config_path)
        phase = "MANIFEST"
        row = load_private_manifest(manifest_path)
        phase = "CREDENTIAL"
        credential_ready(cfg, interactive=True)
        phase = "CONNECT"
        engine = make_engine(cfg, database=FROZEN_DATABASE)
        with engine.connect() as conn:
            # SQLAlchemy rolls back automatically on any error unless commit
            # is reached. For preflight, no INSERT/DDL is issued.
            with conn.begin() as transaction:
                phase = "IDENTITY"
                assert_identity(conn, cfg)
                phase = "CLINICAL_ANCHORS"
                assert_clinical_anchors(conn)
                phase = "CDM_SOURCE_EMPTY"
                assert_empty(conn)
                phase = "METADATA_SCHEMA"
                assert_metadata_schema(conn)
                phase = "VOCABULARY"
                assert_vocabulary(conn, row)
                phase = "CDM_VERSION_CONCEPT"
                assert_concept(conn, row)
                if execute:
                    phase = "INSERT_PERMISSION"
                    assert_insert_right(conn)
                    phase = "INSERT"
                    # Exactly one known table, fixed column names, bound values.
                    # HOLDLOCK prevents a concurrent second insert.
                    columns = ", ".join(f"[{name}]" for name in FIELDS)
                    values = ", ".join(f":{name}" for name in FIELDS)
                    stmt = text(
                        f"INSERT INTO [dbo].[cdm_source] ({columns}) "
                        f"SELECT {values} "
                        "WHERE NOT EXISTS (SELECT 1 FROM [dbo].[cdm_source] "
                        "WITH (UPDLOCK, HOLDLOCK))"
                    )
                    params = {
                        name: (date.fromisoformat(row[name])
                               if name in ("source_release_date", "cdm_release_date")
                               else row[name])
                        for name in FIELDS
                    }
                    result = conn.execute(stmt, params)
                    if result.rowcount != 1:
                        raise AmendmentBlocked("INSERT")
                    phase = "POST_VERIFY"
                    if conn.exec_driver_sql(
                        "SELECT COUNT_BIG(*) FROM [dbo].[cdm_source]"
                    ).scalar_one() != 1:
                        raise AmendmentBlocked("POST_VERIFY")
                    saved = conn.exec_driver_sql(
                        "SELECT " + columns + " FROM [dbo].[cdm_source]"
                    ).mappings().one()
                    for name in FIELDS:
                        if saved[name] != params[name]:
                            raise AmendmentBlocked("POST_VERIFY")
                    assert_clinical_anchors(conn)
                    transaction.commit()
                else:
                    transaction.rollback()
        if execute:
            print("FROZEN CDM_SOURCE METADATA AMENDMENT: PASS")
            print("Exactly one row inserted; frozen clinical anchors unchanged.")
            print("Post-freeze metadata-only change; no ETL rerun or clinical-table write.")
        else:
            print("FROZEN CDM_SOURCE METADATA PREFLIGHT: PASS")
            print("SELECT-only checks passed; no database row inserted.")
    except Exception:
        print("FROZEN CDM_SOURCE METADATA "
              + ("AMENDMENT" if execute else "PREFLIGHT")
              + ": BLOCKED [" + safe_phase(phase) + "]", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview, preflight or explicitly insert one CDM_SOURCE metadata row")
    parser.add_argument("--config", default="config/etl_A.yaml")
    parser.add_argument("--manifest", default="results/postfreeze_standard_qc/cdm_source_manifest.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preflight", action="store_true",
                       help="SELECT-only SQL verification, no insert")
    modes.add_argument("--execute", action="store_true",
                       help="Insert one row in a transaction after explicit confirmation")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.execute:
        if args.confirm != CONFIRM:
            raise SystemExit("METADATA AMENDMENT: BLOCKED [CONFIRMATION]")
        perform(args.config, args.manifest, execute=True)
    elif args.preflight:
        perform(args.config, args.manifest, execute=False)
    else:
        print("PREVIEW ONLY: target OMOP_VALIDATED_A.dbo.cdm_source")
        print("One factual metadata row only; clinical tables must remain unchanged.")
        print("Run --preflight after completing owner-only local provenance manifest.")
        print("No SQL connection or write was attempted.")


if __name__ == "__main__":
    main()
