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
    def stages(self) -> list[str]:
        return list(self.raw.get("stages", []))

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

    if str(raw["etl"].get("cdm_version")) != "5.4":
        raise ValueError("ETL v1 is pinned to OMOP CDM 5.4")

    return EtlConfig(raw=raw, path=path)
