from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import EtlConfig


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_run_manifest(config: EtlConfig, *, status: str = "initialized") -> dict[str, Any]:
    sql = config.raw["sqlserver"]
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "config_path": str(config.path),
        "config_sha256": _sha256_file(config.path),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "etl": {
            "cdm_version": str(config.raw["etl"]["cdm_version"]),
            "backend": str(config.raw["etl"]["backend"]),
            "interactive": bool(config.raw["etl"].get("interactive", True)),
        },
        "source": {
            "parquet_dir": str(config.source_dir),
        },
        "target": {
            "server": str(sql["server"]),
            "database": str(sql["database"]),
            "schema": str(sql.get("target_schema", "dbo")),
        },
        "vocabulary": {
            "directory": str(config.vocabulary_dir),
        },
        "policies": dict(config.raw.get("policies", {}) or {}),
        "stages": list(config.stages),
    }


def write_run_manifest(config: EtlConfig, *, status: str = "initialized") -> Path:
    path = config.audit_dir / "run_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_run_manifest(config, status=status), indent=2),
        encoding="utf-8",
    )
    return path
