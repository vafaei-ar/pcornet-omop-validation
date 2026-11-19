#!/usr/bin/env python3
"""Populate only verifiable CDM_SOURCE fields in a private local JSON.

SELECTs vocabulary/concept metadata from the existing frozen SQL Server.
Never INSERTs, performs DDL, reads clinical rows, or prints metadata values.
Preserves all existing human-entered values; refuses conflicting values.
Source and ETL release dates and the data holder are not guessed.
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.postfreeze_restricted_qc import (
    FROZEN_DATABASE, verified_server_config,
)

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIR = (ROOT / "results" / "postfreeze_standard_qc").resolve()
PRIVATE_FILE = PRIVATE_DIR / "cdm_source_manifest.json"
FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
DOCS_URL = "https://pcornet.org/data/common-data-model/"
ETL_URL = "https://github.com/vafaei-ar/pcornet-omop-validation/tree/" + FROZEN_SHA
KNOWN = {
    "cdm_source_name": "PCORnet-to-OMOP validation database",
    "cdm_source_abbreviation": "PCORnetOMOP",
    "source_description": (
        "Research PCORnet data transformed to OMOP CDM v5.4.2 for an "
        "audited scientific reproducibility study. This is a limited-domain "
        "implementation. The frozen ETL omits diagnoses lacking DX_DATE; "
        "post-freeze CDM_SOURCE metadata added for DQD without changing "
        "clinical tables."
    ),
    "source_documentation_reference": DOCS_URL,
    "cdm_etl_reference": ETL_URL,
    "cdm_version": "v5.4",
    "cdm_version_concept_id": 756265,
}
EVIDENCE = {
    "cdm_version_concept_id": (
        "OMOP_VALIDATED_A.dbo.concept: concept_id=756265; "
        "vocabulary_id=CDM; concept_class_id=CDM; domain_id=Metadata"
    ),
    "vocabulary_version": (
        "OMOP_VALIDATED_A.dbo.vocabulary: vocabulary_id='None', "
        "verified by SELECT-only inspection"
    ),
}
MISSING_FIELDS = ("cdm_holder", "source_release_date", "cdm_release_date")
MISSING_EVIDENCE = MISSING_FIELDS


class PreparationBlocked(RuntimeError):
    pass


def private_file() -> Path:
    if PRIVATE_DIR.is_symlink() or not PRIVATE_DIR.is_dir():
        raise PreparationBlocked("PRIVATE_DIRECTORY")
    if stat.S_IMODE(PRIVATE_DIR.stat().st_mode) != 0o700:
        raise PreparationBlocked("PRIVATE_DIRECTORY")
    if PRIVATE_FILE.is_symlink() or not PRIVATE_FILE.is_file():
        raise PreparationBlocked("PRIVATE_FILE")
    if stat.S_IMODE(PRIVATE_FILE.stat().st_mode) != 0o600:
        raise PreparationBlocked("PRIVATE_FILE")
    if PRIVATE_FILE.stat().st_size > 16384:
        raise PreparationBlocked("PRIVATE_FILE")
    return PRIVATE_FILE


def read_manifest() -> dict:
    path = private_file()
    with path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if (type(manifest) is not dict
        or set(manifest) != {"cdm_source", "evidence"}
        or type(manifest["cdm_source"]) is not dict
        or type(manifest["evidence"]) is not dict
        or set(manifest["cdm_source"]) != {
            "cdm_source_name", "cdm_source_abbreviation", "cdm_holder",
            "source_description", "source_documentation_reference",
            "cdm_etl_reference", "source_release_date", "cdm_release_date",
            "cdm_version", "cdm_version_concept_id", "vocabulary_version"
        }):
        raise PreparationBlocked("MANIFEST_SCHEMA")
    return manifest


def verified_vocabulary(config_path: str, *, prompt_for_secret: bool = True) -> str:
    try:
        verified_server_config(config_path)
        cfg = load_etl_config(config_path)
    except Exception:
        raise PreparationBlocked("CONFIG") from None
    env_key = cfg.raw["sqlserver"].get("password_env")
    if (type(env_key) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key) is None):
        raise PreparationBlocked("CREDENTIAL")
    if not cfg.sql_password:
        if not prompt_for_secret or not sys.stdin.isatty():
            raise PreparationBlocked("CREDENTIAL")
        try:
            secret = getpass.getpass("Authorized existing ETL SQL password (hidden): ")
        except Exception:
            raise PreparationBlocked("CREDENTIAL") from None
        if not secret:
            raise PreparationBlocked("CREDENTIAL")
        os.environ[env_key] = secret
        del secret
    engine = None
    try:
        try:
            engine = make_engine(cfg, database=FROZEN_DATABASE)
            connection = engine.connect()
        except Exception:
            raise PreparationBlocked("SQL_CONNECT") from None
        with connection as conn:
            try:
                identity = conn.exec_driver_sql(
                    "SELECT DB_NAME(), ORIGINAL_LOGIN()"
                ).one()
            except Exception:
                raise PreparationBlocked("IDENTITY_QUERY") from None
            if (str(identity[0]).casefold() != FROZEN_DATABASE.casefold()
                    or str(identity[1]).casefold() !=
                    str(cfg.raw["sqlserver"]["user"]).casefold()):
                raise PreparationBlocked("DATABASE_IDENTITY")
            try:
                vocab = conn.exec_driver_sql(
                    "SELECT vocabulary_version FROM [dbo].[vocabulary] "
                    "WHERE vocabulary_id = 'None'"
                ).all()
            except Exception:
                raise PreparationBlocked("VOCABULARY_QUERY") from None
            if (len(vocab) != 1 or not isinstance(vocab[0][0], str)
                    or not vocab[0][0].strip()):
                raise PreparationBlocked("VOCABULARY_METADATA")
            try:
                concept = conn.execute(text(
                    "SELECT vocabulary_id, concept_class_id, domain_id "
                    "FROM [dbo].[concept] WHERE concept_id=756265"
                )).all()
            except Exception:
                raise PreparationBlocked("CDM_VERSION_QUERY") from None
            if (len(concept) != 1 or tuple(concept[0]) !=
                    ("CDM", "CDM", "Metadata")):
                raise PreparationBlocked("CDM_VERSION_CONCEPT")
            return vocab[0][0]
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def fill_from_verified(manifest: dict, vocabulary: str) -> dict:
    """Preserve any valid hand-entered description; never silently overwrite."""
    data = json.loads(json.dumps(manifest))
    fields = data["cdm_source"]
    evidence = data["evidence"]
    if not isinstance(evidence, dict):
        raise PreparationBlocked("MANIFEST_SCHEMA")
    known = dict(KNOWN, vocabulary_version=vocabulary)
    strict = {"cdm_version", "cdm_version_concept_id", "vocabulary_version"}
    limits = {
        "cdm_source_name": 255,
        "cdm_source_abbreviation": 25,
        "source_documentation_reference": 255,
        "cdm_etl_reference": 255,
    }
    for field, proposed in known.items():
        existing = fields[field]
        if field == "source_description":
            # A real PCORnet source is established by the locked ETL. Keep
            # the operator's wording while adding this required label if
            # missing; case differences and surrounding spaces are harmless.
            if existing is None or (type(existing) is str and not existing.strip()):
                fields[field] = proposed
            elif type(existing) is not str or "\x00" in existing:
                raise PreparationBlocked("INVALID_SOURCE_DESCRIPTION")
            else:
                description = existing.strip()
                if "pcornet" not in description.casefold():
                    description = "PCORnet-derived data. " + description
                fields[field] = description
            continue
        if existing in ("", None):
            fields[field] = proposed
            continue
        if field in strict:
            if existing != proposed:
                raise PreparationBlocked("CONFLICT_" + field.upper())
        elif (type(existing) is not str or not existing.strip()
              or existing != existing.strip() or "\x00" in existing):
            raise PreparationBlocked("INVALID_" + field.upper())
        elif field in limits and len(existing) > limits[field]:
            raise PreparationBlocked("INVALID_" + field.upper())
        elif field == "cdm_etl_reference" and FROZEN_SHA not in existing:
            raise PreparationBlocked("CONFLICT_CDM_ETL_REFERENCE")
        # Otherwise retain the user's actual text exactly; it may be a
        # more accurate institutional source description than our default.
    for field, proposed in EVIDENCE.items():
        existing = evidence.get(field, "")
        if existing in ("", None):
            evidence[field] = proposed
        elif (type(existing) is not str or not existing.strip()
              or existing != existing.strip() or len(existing) > 255):
            raise PreparationBlocked("INVALID_EVIDENCE_" + field.upper())
        # Human-entered evidence references are preserved, not replaced.
    return data


def save_private_manifest(manifest: dict) -> None:
    path = private_file()
    # Atomically replace one owner-only, ordinary file in its approved directory.
    # This intentionally updates only a user's existing blank/partly complete
    # private manifest and never the tracked example.
    handle = None
    temporary = None
    try:
        handle, temporary = tempfile.mkstemp(
            prefix=".cdm_source_manifest_", suffix=".json", dir=PRIVATE_DIR
        )
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            handle = None
            json.dump(manifest, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Fill verified CDM_SOURCE fields in owner-only local manifest; no database writes"
    )
    p.add_argument("--config", default="config/etl_A.yaml")
    p.add_argument("--execute-local", action="store_true",
                   help="SELECT-only SQL metadata read, then update private JSON file")
    args = p.parse_args()
    if not args.execute_local:
        print("PREVIEW ONLY: SQL vocabulary/concept read + private manifest update.")
        print("Never writes database tables or prints protected metadata values.")
        print("Use --execute-local to fill verifiable values in the private file.")
        return
    phase = "MANIFEST"
    try:
        manifest = read_manifest()
        phase = "METADATA_CONNECT"
        vocabulary = verified_vocabulary(args.config)
        phase = "MERGE"
        populated = fill_from_verified(manifest, vocabulary)
        phase = "PRIVATE_FILE_WRITE"
        save_private_manifest(populated)
    except PreparationBlocked as blocked:
        stage = str(blocked)
        if not re.fullmatch(r"[A-Z0-9_]+", stage):
            stage = phase
        print("FROZEN CDM_SOURCE PRIVATE PREFILL: BLOCKED [" + stage + "]",
              file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("FROZEN CDM_SOURCE PRIVATE PREFILL: BLOCKED [" + phase + "]",
              file=sys.stderr)
        raise SystemExit(1) from None
    print("FROZEN CDM_SOURCE PRIVATE PREFILL: PASS")
    print("Known descriptive, ETL, CDM concept and vocabulary metadata filled.")
    print("Existing source description retained; PCORnet provenance label normalized where necessary.")
    print("Still needs verified holder, source release date and CDM release date,")
    print("with original evidence references. No SQL database rows modified.")


if __name__ == "__main__":
    main()
