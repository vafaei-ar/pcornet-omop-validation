from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import EtlConfig, save_etl_config

ATHENA_URL = "https://athena.ohdsi.org"
DEFAULT_ATHENA_DIR = Path("/usr/local/datasets/OMOP/Athena")
DEFAULT_DISCOVERY_ROOTS = (Path("/usr/local/datasets/OMOP"),)


@dataclass(frozen=True)
class AthenaBundle:
    archive: Path
    directory: Path
    sha256: str
    vocabulary_versions: dict[str, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"Unsafe archive member: {member.filename}")
        zf.extractall(destination)


def _find_vocab_root(directory: Path) -> Path:
    if (directory / "CONCEPT.csv").exists():
        return directory
    matches = list(directory.rglob("CONCEPT.csv"))
    if len(matches) == 1:
        return matches[0].parent
    if not matches:
        raise ValueError("Athena archive does not contain CONCEPT.csv")
    raise ValueError("Athena archive contains multiple possible vocabulary roots")


def _read_vocabulary_versions(directory: Path) -> dict[str, str]:
    path = directory / "VOCABULARY.csv"
    if not path.exists():
        return {}

    versions: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        for row in reader:
            vocab_id = (row.get("vocabulary_id") or row.get("VOCABULARY_ID") or "").strip()
            version = (row.get("vocabulary_version") or row.get("VOCABULARY_VERSION") or "").strip()
            if vocab_id:
                versions[vocab_id] = version
    return versions


def _validate_required_files(config: EtlConfig, directory: Path) -> None:
    required = config.raw.get("vocabulary", {}).get("require_files", [])
    missing = [name for name in required if not (directory / name).exists()]
    if missing:
        raise ValueError("Athena bundle is missing required files: " + ", ".join(missing))


def _validate_required_vocabularies(config: EtlConfig, versions: dict[str, str]) -> None:
    required = config.raw.get("vocabulary", {}).get("require_vocabularies", [])
    missing = [name for name in required if name not in versions]
    if missing:
        raise ValueError(
            "Athena bundle is missing required vocabularies: " + ", ".join(missing)
        )


def _download_bundle(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, destination)
    return destination


def _record_existing_vocabulary(config: EtlConfig, directory: Path) -> None:
    versions = _read_vocabulary_versions(directory)
    _validate_required_files(config, directory)
    _validate_required_vocabularies(config, versions)
    config.raw.setdefault("vocabulary", {})["directory"] = str(directory)
    save_etl_config(config)

    manifest = config.audit_dir / "athena_vocabulary.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "source": "existing_local_installation",
                "directory": str(directory),
                "required_vocabularies": config.raw.get("vocabulary", {}).get(
                    "require_vocabularies", []
                ),
                "vocabulary_versions": versions,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _discovery_roots(config: EtlConfig) -> list[Path]:
    values = config.raw.setdefault("vocabulary", {}).get("discovery_roots")
    if not values:
        values = [str(path) for path in DEFAULT_DISCOVERY_ROOTS]
    return [Path(value).expanduser() for value in values]


def _discover_existing_vocabulary(config: EtlConfig) -> Path | None:
    candidates: list[Path] = []
    for root in _discovery_roots(config):
        if not root.is_dir():
            continue
        for concept_file in root.rglob("CONCEPT.csv"):
            candidate = concept_file.parent
            try:
                _validate_required_files(config, candidate)
                versions = _read_vocabulary_versions(candidate)
                _validate_required_vocabularies(config, versions)
            except ValueError:
                continue
            candidates.append(candidate)

    unique = sorted({candidate.resolve() for candidate in candidates})
    if not unique:
        return None
    if len(unique) > 1:
        joined = "\n  - ".join(str(path) for path in unique)
        raise ValueError(
            "Multiple valid Athena vocabulary directories were found. "
            "Set vocabulary.directory explicitly to one of:\n  - " + joined
        )
    return unique[0]


def _zip_has_required_files(config: EtlConfig, archive: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as zf:
            basenames = {Path(name).name for name in zf.namelist() if not name.endswith("/")}
    except (OSError, zipfile.BadZipFile):
        return False
    required = set(config.raw.get("vocabulary", {}).get("require_files", []))
    return bool(required) and required.issubset(basenames)


def _discover_existing_archive(config: EtlConfig) -> Path | None:
    candidates: list[Path] = []
    for root in _discovery_roots(config):
        if not root.is_dir():
            continue
        for archive in root.rglob("*.zip"):
            if _zip_has_required_files(config, archive):
                candidates.append(archive.resolve())

    unique = sorted(set(candidates))
    if not unique:
        return None
    if len(unique) > 1:
        joined = "\n  - ".join(str(path) for path in unique)
        raise ValueError(
            "Multiple possible Athena vocabulary ZIP archives were found. "
            "Set vocabulary.directory to an extracted bundle or provide the desired ZIP explicitly:\n  - "
            + joined
        )
    return unique[0]


def _valid_download_archives(config: EtlConfig, download_dir: Path) -> list[Path]:
    if not download_dir.is_dir():
        return []
    archives = [path for path in download_dir.glob("*.zip") if _zip_has_required_files(config, path)]
    return sorted(archives, key=lambda path: path.stat().st_mtime, reverse=True)


def _resolve_target_directory(config: EtlConfig) -> Path:
    vocab = config.raw.setdefault("vocabulary", {})
    configured = str(vocab.get("directory") or "").strip()

    # Honor an explicitly configured installation only when it actually contains
    # the Athena vocabulary. A prior failed acquisition may have rewritten the
    # placeholder to DEFAULT_ATHENA_DIR even though no files were installed there.
    if configured not in {"", "/path/to/athena"}:
        configured_path = Path(configured).expanduser()
        if (configured_path / "CONCEPT.csv").exists():
            return configured_path

    # If the configured location is absent/incomplete, search known local roots
    # before falling back to Athena download. This handles installations copied
    # under names such as /usr/local/datasets/OMOP/vocabulary.
    discovered = _discover_existing_vocabulary(config)
    if discovered is not None:
        _record_existing_vocabulary(config, discovered)
        return discovered

    # Preserve a genuinely explicit non-placeholder target as the eventual install
    # destination. Otherwise use the standard default target directory.
    if configured not in {"", "/path/to/athena"}:
        return Path(configured).expanduser()

    vocab["directory"] = str(DEFAULT_ATHENA_DIR)
    save_etl_config(config)
    return DEFAULT_ATHENA_DIR


def _install_archive(config: EtlConfig, archive: Path, target: Path, cache_dir: Path) -> AthenaBundle:
    if not archive.exists():
        raise ValueError(f"Athena ZIP does not exist: {archive}")
    if not _zip_has_required_files(config, archive):
        raise ValueError(f"ZIP is not a valid Athena vocabulary bundle: {archive}")

    archive_hash = _sha256(archive)
    temp_extract = cache_dir / "Athena" / f"extract-{archive_hash[:12]}"
    if temp_extract.exists():
        shutil.rmtree(temp_extract)
    temp_extract.mkdir(parents=True, exist_ok=True)
    _safe_extract_zip(archive, temp_extract)
    root = _find_vocab_root(temp_extract)
    _validate_required_files(config, root)
    versions = _read_vocabulary_versions(root)
    _validate_required_vocabularies(config, versions)

    if target.exists() and any(target.iterdir()):
        raise ValueError(f"Refusing to overwrite non-empty Athena directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for item in root.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)

    config.raw.setdefault("vocabulary", {})["directory"] = str(target)
    save_etl_config(config)

    manifest = config.audit_dir / "athena_vocabulary.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "source": "local_or_downloaded_bundle",
                "archive": str(archive),
                "archive_sha256": archive_hash,
                "directory": str(target),
                "required_vocabularies": config.raw.get("vocabulary", {}).get(
                    "require_vocabularies", []
                ),
                "vocabulary_versions": versions,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return AthenaBundle(
        archive=archive,
        directory=target,
        sha256=archive_hash,
        vocabulary_versions=versions,
    )


def acquire_athena_vocabulary(config: EtlConfig) -> AthenaBundle | None:
    """Acquire or discover and validate an Athena vocabulary bundle.

    Search order: extracted local vocabulary, local Athena ZIP, authorized bundle URL,
    then interactive Athena download. Arbitrary ZIP files are never auto-selected.
    """
    vocab = config.raw.setdefault("vocabulary", {})
    target = _resolve_target_directory(config)

    if (target / "CONCEPT.csv").exists():
        root = _find_vocab_root(target)
        _record_existing_vocabulary(config, root)
        return None

    cache_dir = Path(config.raw.get("downloads", {}).get("cache_dir", ".cache/pcornet-omop-etl"))

    local_archive = _discover_existing_archive(config)
    if local_archive is not None:
        print(f"Found local Athena vocabulary ZIP: {local_archive}")
        return _install_archive(config, local_archive, target, cache_dir)

    acquisition = vocab.setdefault("acquisition", {})
    bundle_url_env = str(acquisition.get("bundle_url_env", "ATHENA_BUNDLE_URL"))
    bundle_url = os.environ.get(bundle_url_env)
    download_dir = Path(acquisition.get("download_dir", "~/Downloads")).expanduser()
    cached_zip = cache_dir / "Athena" / "athena-vocabulary.zip"

    if bundle_url:
        archive = _download_bundle(bundle_url, cached_zip)
        return _install_archive(config, archive, target, cache_dir)

    valid_downloads = _valid_download_archives(config, download_dir)
    if len(valid_downloads) == 1:
        archive = valid_downloads[0]
        print(f"Found Athena vocabulary ZIP in Downloads: {archive}")
        return _install_archive(config, archive, target, cache_dir)
    if len(valid_downloads) > 1:
        joined = "\n  - ".join(str(path) for path in valid_downloads)
        raise ValueError(
            "Multiple valid Athena vocabulary ZIP files were found in Downloads. "
            "Provide the desired ZIP path explicitly:\n  - " + joined
        )

    if not config.interactive:
        raise ValueError(
            "Athena vocabulary is absent and no authorized local/download bundle was found. "
            f"Set {bundle_url_env} to an authorized Athena bundle URL or run interactively."
        )

    required = vocab.get("require_vocabularies", [])
    print("Athena vocabulary package is required and was not found locally.")
    print("Required vocabularies: " + ", ".join(required))
    print("Athena authentication/licensing cannot be bypassed by the ETL.")
    print("A browser will be opened. Sign in, select/download the required vocabulary bundle,")
    print("then return here and provide the downloaded ZIP path.")
    webbrowser.open(ATHENA_URL)
    entered = input("Path to downloaded Athena ZIP: ").strip()
    if not entered:
        raise ValueError(
            "No Athena ZIP path was provided. The runner will not guess from unrelated ZIP files."
        )
    archive = Path(entered).expanduser()
    return _install_archive(config, archive, target, cache_dir)
