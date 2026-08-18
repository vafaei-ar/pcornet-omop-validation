from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class EtlConfig:
    raw: dict[str, Any]
    path: Path

    @property
    def source_dir(self) -> Path:
        return Path(self.raw["source"]["parquet_dir"])

    @property
    def vocabulary_dir(self) -> Path:
        return Path(self.raw["vocabulary"]["directory"])

    @property
    def output_dir(self) -> Path:
        return Path(self.raw["output"]["parquet_dir"])

    @property
    def audit_dir(self) -> Path:
        return Path(self.raw["output"].get("audit_dir", "results/etl_audit"))

    @property
    def stages(self) -> list[str]:
        return list(self.raw.get("stages", []))

    @property
    def interactive(self) -> bool:
        return bool(self.raw.get("etl", {}).get("interactive", True))

    @property
    def sql_password(self) -> str | None:
        env_name = self.raw.get("sqlserver", {}).get("password_env")
        return os.environ.get(env_name) if env_name else None


def load_etl_config(path: str | Path) -> EtlConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    required_sections = ["etl", "source", "sqlserver", "vocabulary", "output"]
    missing = [name for name in required_sections if name not in raw]
    if missing:
        raise ValueError(f"Missing ETL configuration sections: {', '.join(missing)}")

    if raw["etl"].get("backend") != "sqlserver":
        raise ValueError("ETL v1 currently supports backend='sqlserver' only")

    cdm_version = str(raw["etl"].get("cdm_version", ""))
    if not cdm_version.startswith("5.4"):
        raise ValueError("ETL v1 is pinned to the OMOP CDM 5.4 series")

    return EtlConfig(raw=raw, path=path)


def save_etl_config(config: EtlConfig) -> None:
    with config.path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config.raw, handle, sort_keys=False)
