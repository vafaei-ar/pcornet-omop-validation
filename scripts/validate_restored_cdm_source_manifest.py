#!/usr/bin/env python3
"""Offline, read-only validation of provenance for a CDM_SOURCE metadata row.

No SQL connection, SQL write, backup/restore, or file modification. The manifest
must stay in the institution's ignored, access-restricted results directory.
This script only checks completeness and syntax, not truth of the assertions.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import stat
import sys

BASE = Path("results/postfreeze_standard_qc").resolve()
FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
FIELD_LIMITS = {
    "cdm_source_name": 255,
    "cdm_source_abbreviation": 25,
    "cdm_holder": 255,
    "source_description": None,
    "source_documentation_reference": 255,
    "cdm_etl_reference": 255,
    "source_release_date": 10,
    "cdm_release_date": 10,
    "cdm_version": 10,
    "cdm_version_concept_id": None,
    "vocabulary_version": 20,
}
REQUIRED = frozenset({
    "cdm_source_name", "cdm_source_abbreviation", "cdm_holder",
    "source_release_date", "cdm_release_date", "cdm_version",
    "cdm_version_concept_id", "vocabulary_version",
})
REQUIRED_EVIDENCE = frozenset({
    "cdm_holder", "source_release_date", "cdm_release_date",
    "cdm_version_concept_id", "vocabulary_version",
})
MAX_BYTES = 16384


class ManifestBlocked(ValueError):
    pass


def validate(record: dict) -> None:
    if type(record) is not dict or set(record) != {"cdm_source", "evidence"}:
        raise ManifestBlocked("Manifest must contain exactly cdm_source and evidence")
    row, evidence = record["cdm_source"], record["evidence"]
    if type(row) is not dict or set(row) != set(FIELD_LIMITS):
        raise ManifestBlocked("CDM_SOURCE must have exactly 11 v5.4 columns")
    if type(evidence) is not dict or not REQUIRED_EVIDENCE.issubset(evidence):
        raise ManifestBlocked("Missing required provenance evidence references")
    for field, limit in FIELD_LIMITS.items():
        value = row[field]
        if field == "cdm_version_concept_id":
            if type(value) is not int or value != 756265:
                raise ManifestBlocked("Unverified v5.4 CDM version concept")
            continue
        if value is not None and (type(value) is not str
                                  or (limit is not None and len(value) > limit)
                                  or "\x00" in value):
            raise ManifestBlocked("Invalid CDM_SOURCE field or length")
        if field in REQUIRED and (not value or not value.strip()
                                  or value.strip() != value):
            raise ManifestBlocked("Missing mandatory CDM_SOURCE value")
    if row["cdm_version"] != "v5.4":
        raise ManifestBlocked("Use official v5.4 CDM version label")
    for field in ("source_release_date", "cdm_release_date"):
        value = row[field]
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ManifestBlocked("Invalid factual release date") from None
        if parsed.isoformat() != value:
            raise ManifestBlocked("Use YYYY-MM-DD release dates")
    # No invented dates or values. A reference is mandatory but still
    # requires human confirmation against original institution records.
    for field in REQUIRED_EVIDENCE:
        value = evidence[field]
        if (type(value) is not str or not value.strip()
                or len(value) > 255 or value.strip() != value):
            raise ManifestBlocked("Missing verifiable metadata provenance")
    if row["cdm_etl_reference"]:
        if FROZEN_SHA not in row["cdm_etl_reference"]:
            raise ManifestBlocked("ETL reference must identify frozen SHA")
    if not row["source_description"] or "PCORnet" not in row["source_description"]:
        raise ManifestBlocked("Source description must identify PCORnet")
    if not row["source_documentation_reference"]:
        raise ManifestBlocked("Missing source documentation reference")
    if not row["cdm_etl_reference"]:
        raise ManifestBlocked("Missing frozen ETL reference")


def validate_private_path(path: Path) -> None:
    real = path.resolve(strict=True)
    if real.parent != BASE or path.is_symlink() or not real.is_file():
        raise ManifestBlocked("Manifest outside approved private directory")
    if stat.S_IMODE(real.stat().st_mode) != 0o600:
        raise ManifestBlocked("Manifest must be owner-only (0600)")
    if stat.S_IMODE(real.parent.stat().st_mode) != 0o700:
        raise ManifestBlocked("Parent directory must be owner-only (0700)")
    if real.stat().st_size > MAX_BYTES:
        raise ManifestBlocked("Manifest exceeds safe size")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate local CDM_SOURCE provenance only")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        path = Path(args.manifest)
        validate_private_path(path)
        with path.open("r", encoding="utf-8") as stream:
            record = json.load(stream)
        validate(record)
    except Exception:
        print("LOCAL CDM_SOURCE PROVENANCE: BLOCKED "
              "(details suppressed; verify dates, holder and source evidence locally)",
              file=sys.stderr)
        raise SystemExit(1) from None
    print("LOCAL CDM_SOURCE PROVENANCE: PRELIMINARY PASS")
    print("No database was accessed or modified; human source and authorized-target checks remain required.")


if __name__ == "__main__":
    main()
