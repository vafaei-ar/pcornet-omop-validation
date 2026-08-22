from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import EtlConfig


CRITICAL_MODULES = (
    "src/pcornet_omop_validation/etl/preflight.py",
    "src/pcornet_omop_validation/etl/staging.py",
    "src/pcornet_omop_validation/etl/visit_occurrence_validated.py",
    "src/pcornet_omop_validation/etl/condition_occurrence.py",
    "src/pcornet_omop_validation/etl/procedure_event_routes.py",
    "src/pcornet_omop_validation/etl/procedure_occurrence.py",
    "src/pcornet_omop_validation/etl/measurement.py",
    "src/pcornet_omop_validation/etl/obs_clin_routes.py",
    "src/pcornet_omop_validation/etl/measurement_obs_clin_append.py",
    "src/pcornet_omop_validation/etl/observation.py",
    "src/pcornet_omop_validation/etl/condition_obs_clin_append.py",
    "src/pcornet_omop_validation/etl/drug_event_routes.py",
    "src/pcornet_omop_validation/etl/drug_exposure.py",
    "src/pcornet_omop_validation/etl/drug_route_finalize.py",
    "src/pcornet_omop_validation/etl/procedure_remaining_domains.py",
    "src/pcornet_omop_validation/etl/death.py",
    "src/pcornet_omop_validation/etl/global_reconciliation.py",
)

PRINCIPLE = (
    "Primary PCORnet-to-OMOP ETL rules must be generalizable and based on PCORnet CDM "
    "semantics, OMOP CDM conventions, and standard vocabularies. Dataset-specific, "
    "phenotype-aware, or discrepancy-driven reconciliation patches are not permitted in "
    "the primary ETL."
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def audit_freeze_readiness(config: EtlConfig, repo_root: str | Path = ".") -> dict[str, object]:
    repo = Path(repo_root).resolve()
    audit_path = config.audit_dir / "freeze_readiness.json"

    commit = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(repo, "status", "--porcelain")

    status_by_path: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line:
            continue
        path = line[3:]
        status_by_path[path] = line[:2]

    tracked = set(_git(repo, "ls-files").splitlines())
    module_status = []
    blockers = []

    for path in CRITICAL_MODULES:
        exists = (repo / path).exists()
        is_tracked = path in tracked
        dirty_status = status_by_path.get(path)
        module_status.append(
            {
                "path": path,
                "exists": exists,
                "tracked": is_tracked,
                "dirty_status": dirty_status,
            }
        )
        if not exists:
            blockers.append(f"missing critical ETL module: {path}")
        elif not is_tracked:
            blockers.append(f"untracked critical ETL module: {path}")
        elif dirty_status:
            blockers.append(f"dirty critical ETL module: {path} ({dirty_status})")

    config_dirty = [
        {"path": p, "status": s}
        for p, s in sorted(status_by_path.items())
        if p.startswith("config/")
    ]

    payload = {
        "stage": "freeze_readiness",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "principle": PRINCIPLE,
        "git": {
            "branch": branch,
            "commit": commit,
            "working_tree_clean": not bool(porcelain),
        },
        "critical_modules": module_status,
        "config_changes_not_committed": config_dirty,
        "blockers": blockers,
        "status": "ready" if not blockers else "not_ready",
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {**payload, "audit_path": str(audit_path)}
