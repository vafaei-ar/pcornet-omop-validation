from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import EtlConfig
from .database import check_connection


REQUIRED_SOURCE_TABLES = {
    "demographic.parquet",
    "enrollment.parquet",
    "encounter.parquet",
    "diagnosis.parquet",
    "procedures.parquet",
    "vital.parquet",
    "prescribing.parquet",
    "dispensing.parquet",
    "lab_result_cm.parquet",
    "med_admin.parquet",
    "obs_clin.parquet",
    "obs_gen.parquet",
    "condition.parquet",
    "death.parquet",
    "death_cause.parquet",
    "immunization.parquet",
    "lds_address_history.parquet",
}

OPTIONAL_SOURCE_TABLES = {
    "provider.parquet",
}


@dataclass(frozen=True)
class PreflightResult:
    errors: list[str]
    warnings: list[str]
    found_tables: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _missing_files(directory: Path, names: set[str]) -> list[str]:
    return sorted(name for name in names if not (directory / name).exists())


def _missing_source_files(
    directory: Path,
    logical_names: set[str],
    table_prefix: str,
) -> list[str]:
    """Return missing logical PCORnet parquet names.

    Source exports in this project use names such as ``PCORnet_DIAGNOSIS.parquet``
    while the logical registry is intentionally prefix-free.  Preflight therefore
    resolves both prefixed and unprefixed filenames and compares case-insensitively
    on case-sensitive Linux filesystems.  This keeps preflight aligned with the
    configured ``source.table_prefix`` rather than requiring users to rename files.
    """
    found = {
        path.name.casefold(): path.name
        for path in directory.glob("*.parquet")
        if path.is_file()
    }
    prefix = str(table_prefix or "")
    missing: list[str] = []
    for logical in sorted(logical_names):
        candidates = {logical.casefold()}
        if prefix:
            candidates.add(f"{prefix}{logical}".casefold())
        if not any(candidate in found for candidate in candidates):
            missing.append(logical)
    return missing


def _athena_vocabulary_ids(vocabulary_csv: Path) -> set[str]:
    """Read vocabulary IDs from Athena VOCABULARY.csv without loading large tables."""
    if not vocabulary_csv.is_file():
        return set()

    with vocabulary_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        handle.seek(0)
        delimiter = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            return set()
        normalized = {str(name).strip().upper(): name for name in reader.fieldnames}
        id_field = normalized.get("VOCABULARY_ID")
        if id_field is None:
            return set()
        return {
            str(row.get(id_field, "")).strip()
            for row in reader
            if str(row.get(id_field, "")).strip()
        }


def run_preflight(config: EtlConfig) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []

    source_dir = config.source_dir
    table_prefix = str((config.raw.get("source", {}) or {}).get("table_prefix", ""))
    if not source_dir.is_dir():
        errors.append(f"PCORnet parquet directory does not exist: {source_dir}")
        found_tables: list[str] = []
    else:
        found_tables = sorted(path.name for path in source_dir.glob("*.parquet"))
        missing_required = _missing_source_files(
            source_dir, REQUIRED_SOURCE_TABLES, table_prefix
        )
        if missing_required:
            message = "Missing expected PCORnet tables: " + ", ".join(missing_required)
            if config.raw["etl"].get("fail_on_missing_required_table", True):
                errors.append(message)
            else:
                warnings.append(message)

        missing_optional = _missing_source_files(
            source_dir, OPTIONAL_SOURCE_TABLES, table_prefix
        )
        if missing_optional:
            warnings.append(
                "Optional PCORnet table unavailable: " + ", ".join(missing_optional)
            )

    vocabulary_dir = config.vocabulary_dir
    if not vocabulary_dir.is_dir():
        errors.append(f"Athena vocabulary directory does not exist: {vocabulary_dir}")
    else:
        required_files = set(config.raw["vocabulary"].get("require_files", []))
        missing_files = _missing_files(vocabulary_dir, required_files)
        if missing_files:
            errors.append("Missing Athena vocabulary files: " + ", ".join(missing_files))

        required_ids = {
            str(value).strip()
            for value in config.raw["vocabulary"].get("require_vocabularies", [])
            if str(value).strip()
        }
        if required_ids and "VOCABULARY.csv" not in missing_files:
            available_ids = _athena_vocabulary_ids(vocabulary_dir / "VOCABULARY.csv")
            if not available_ids:
                errors.append(
                    "Could not read vocabulary IDs from Athena VOCABULARY.csv; "
                    "required vocabulary validation cannot be completed."
                )
            else:
                missing_ids = sorted(required_ids - available_ids)
                if missing_ids:
                    errors.append(
                        "Missing required Athena vocabularies: " + ", ".join(missing_ids)
                    )

    if not config.sql_password:
        env_name = config.raw["sqlserver"].get("password_env", "<unset>")
        errors.append(f"SQL Server password environment variable is not set: {env_name}")
    else:
        try:
            status = check_connection(config, database="master")
            warnings.append(
                f"SQL Server connection verified: {status.server}; version {status.server_version}"
            )
        except Exception as exc:
            errors.append(f"SQL Server connectivity/authentication check failed: {exc}")

    if config.raw["etl"].get("reset_target") and config.raw["etl"].get(
        "fail_on_existing_target_rows", True
    ):
        warnings.append(
            "reset_target=true conflicts conceptually with fail_on_existing_target_rows=true; "
            "the runner will require an explicit destructive-action acknowledgement before reset is implemented."
        )

    return PreflightResult(errors=errors, warnings=warnings, found_tables=found_tables)
