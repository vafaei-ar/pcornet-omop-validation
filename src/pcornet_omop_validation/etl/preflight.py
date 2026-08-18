from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import EtlConfig
from .database import check_connection


REQUIRED_SOURCE_TABLES = {
    "PCORnet_DEMOGRAPHIC.parquet",
    "PCORnet_ENROLLMENT.parquet",
    "PCORnet_ENCOUNTER.parquet",
    "PCORnet_DIAGNOSIS.parquet",
    "PCORnet_PROCEDURES.parquet",
    "PCORnet_VITAL.parquet",
    "PCORnet_PRESCRIBING.parquet",
    "PCORnet_DISPENSING.parquet",
    "PCORnet_LAB_RESULT_CM.parquet",
    "PCORnet_MED_ADMIN.parquet",
    "PCORnet_OBS_CLIN.parquet",
    "PCORnet_OBS_GEN.parquet",
    "PCORnet_CONDITION.parquet",
    "PCORnet_DEATH.parquet",
    "PCORnet_DEATH_CAUSE.parquet",
    "PCORnet_IMMUNIZATION.parquet",
    "PCORnet_LDS_ADDRESS_HISTORY.parquet",
}

OPTIONAL_SOURCE_TABLES = {"PCORnet_PROVIDER.parquet"}


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


def run_preflight(config: EtlConfig) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []

    source_dir = config.source_dir
    if not source_dir.is_dir():
        errors.append(f"PCORnet parquet directory does not exist: {source_dir}")
        found_tables: list[str] = []
    else:
        found_tables = sorted(path.name for path in source_dir.glob("*.parquet"))
        missing_required = _missing_files(source_dir, REQUIRED_SOURCE_TABLES)
        if missing_required:
            message = "Missing expected PCORnet tables: " + ", ".join(missing_required)
            if config.raw["etl"].get("fail_on_missing_required_table", True):
                errors.append(message)
            else:
                warnings.append(message)

        missing_optional = _missing_files(source_dir, OPTIONAL_SOURCE_TABLES)
        if missing_optional:
            warnings.append(
                "Optional PCORnet table unavailable: " + ", ".join(missing_optional)
            )

    vocabulary_dir = config.vocabulary_dir
    if not vocabulary_dir.is_dir():
        errors.append(f"Athena vocabulary directory does not exist: {vocabulary_dir}")
    else:
        required_vocab = set(config.raw["vocabulary"].get("require_files", []))
        missing_vocab = _missing_files(vocabulary_dir, required_vocab)
        if missing_vocab:
            errors.append("Missing Athena vocabulary files: " + ", ".join(missing_vocab))

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
