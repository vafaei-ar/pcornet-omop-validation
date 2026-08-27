from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pcornet_omop_validation.etl.config import load_etl_config


FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _git_status(repo_root: Path) -> list[str]:
    value = _git(repo_root, "status", "--porcelain")
    if not value:
        return []
    return [line for line in value.splitlines() if line.strip()]


def write_publication_analysis_manifest(
    config_path: str,
    *,
    study_definition: str,
    output_dir: str | None = None,
) -> Path:
    """Write a provenance manifest for a downstream publication-analysis run.

    The manifest deliberately anchors every analysis to the immutable ETL freeze SHA
    while allowing analysis code to advance independently on the publication branch.
    It records hashes and identifiers only; it does not serialize credentials or the
    contents of the local ETL configuration.
    """
    config_file = Path(config_path).expanduser().resolve()
    if not config_file.is_file():
        raise FileNotFoundError(f"Config file does not exist: {config_file}")

    config = load_etl_config(str(config_file))
    repo_root = Path(__file__).resolve().parents[3]
    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    branch = _git(repo_root, "branch", "--show-current")
    status = _git_status(repo_root)

    sql_cfg = config.raw.get("sqlserver", {}) or {}
    result_root = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else (Path(config.audit_dir).resolve().parent / "publication_analysis" / "manifests")
    )
    result_root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    filename = timestamp.strftime("analysis_manifest_%Y%m%dT%H%M%SZ.json")
    path = result_root / filename

    payload = {
        "recorded_at_utc": timestamp.isoformat(),
        "stage": "publication_analysis_manifest",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_git_branch": branch,
        "analysis_git_status_porcelain": status,
        "analysis_worktree_clean": not status,
        "study_definition": study_definition,
        "config_path": str(config_file),
        "config_sha256": _sha256(config_file),
        "database": sql_cfg.get("database"),
        "source_schema": sql_cfg.get("source_schema", "dbo"),
        "target_schema": sql_cfg.get("target_schema", "dbo"),
        "policy": {
            "etl_freeze": "immutable input to downstream publication analysis",
            "etl_retuning_from_concordance_results": "prohibited",
            "secrets_in_manifest": False,
        },
    }

    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a provenance manifest for a publication-analysis run."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--study-definition",
        required=True,
        help="Short immutable label/version for the analysis or phenotype specification.",
    )
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    path = write_publication_analysis_manifest(
        args.config,
        study_definition=args.study_definition,
        output_dir=args.output_dir,
    )
    print("status: publication_analysis_manifest_written")
    print("frozen_etl_sha:", FROZEN_ETL_SHA)
    print("manifest:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
