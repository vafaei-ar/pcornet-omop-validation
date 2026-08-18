from __future__ import annotations

import hashlib
import json
import tarfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import EtlConfig


@dataclass(frozen=True)
class AcquiredAsset:
    name: str
    version: str
    url: str
    archive: Path
    extracted_dir: Path
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"Unsafe archive member: {member.name}")
        tar.extractall(destination)


def acquire_common_data_model(config: EtlConfig) -> AcquiredAsset:
    downloads = config.raw.get("downloads", {}) or {}
    cdm = downloads.get("common_data_model", {}) or {}
    release = str(cdm.get("release") or f"v{config.raw['etl']['cdm_version']}")
    source = str(cdm.get("source") or "OHDSI/CommonDataModel")
    cache_dir = Path(downloads.get("cache_dir", ".cache/pcornet-omop-etl"))
    asset_dir = cache_dir / "CommonDataModel" / release
    asset_dir.mkdir(parents=True, exist_ok=True)

    archive = asset_dir / f"CommonDataModel-{release}.tar.gz"
    url = f"https://github.com/{source}/archive/refs/tags/{release}.tar.gz"
    if not archive.exists():
        urllib.request.urlretrieve(url, archive)

    sha256 = _sha256(archive)
    extracted = asset_dir / "source"
    if not extracted.exists():
        extracted.mkdir(parents=True, exist_ok=True)
        _safe_extract_tar(archive, extracted)

    return AcquiredAsset(
        name="OHDSI CommonDataModel",
        version=release,
        url=url,
        archive=archive,
        extracted_dir=extracted,
        sha256=sha256,
    )


def acquire_public_dependencies(config: EtlConfig) -> list[AcquiredAsset]:
    downloads = config.raw.get("downloads", {}) or {}
    if not downloads.get("auto_download_public_assets", True):
        return []

    assets = [acquire_common_data_model(config)]
    manifest_path = config.audit_dir / "dependencies.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "assets": [
            {
                "name": asset.name,
                "version": asset.version,
                "url": asset.url,
                "archive": str(asset.archive),
                "extracted_dir": str(asset.extracted_dir),
                "sha256": asset.sha256,
            }
            for asset in assets
        ],
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return assets
