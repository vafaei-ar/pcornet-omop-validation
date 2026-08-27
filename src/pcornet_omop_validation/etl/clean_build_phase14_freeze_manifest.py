from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .clean_build_phase13_review_decisions import run_clean_build_phase13_review_decisions
from .config import EtlConfig, load_etl_config
from .database import make_engine
from .visit_time_semantics_audit import audit_visit_time_semantics


# Auxiliary OMOP concept fields that have deterministic domain semantics and are
# not fully covered by semantic_freeze_audit.py. Type Concept fields are
# intentionally validated there; these checks focus on remaining standardized
# semantic fields that can be validated without inventing source meaning.
AUXILIARY_CONCEPT_CHECKS = (
    ("person", "gender_concept_id", "Gender", True),
    ("person", "race_concept_id", "Race", True),
    ("person", "ethnicity_concept_id", "Ethnicity", True),
    ("visit_occurrence", "visit_concept_id", "Visit", True),
    ("condition_occurrence", "condition_status_concept_id", "Condition Status", True),
    ("measurement", "operator_concept_id", "Operator", True),
    ("measurement", "unit_concept_id", "Unit", True),
    ("observation", "unit_concept_id", "Unit", True),
    ("drug_exposure", "route_concept_id", "Route", True),
    ("device_exposure", "unit_concept_id", "Unit", True),
    ("specimen", "unit_concept_id", "Unit", True),
    ("specimen", "anatomic_site_concept_id", "Spec Anatomic Site", True),
    ("death", "cause_concept_id", "Condition", True),
)

# Fields whose legal domain is context-dependent but whose nonzero value must at
# least resolve to an active Standard concept.
ACTIVE_STANDARD_ONLY = (
    ("measurement", "value_as_concept_id"),
    ("observation", "value_as_concept_id"),
    ("observation", "qualifier_concept_id"),
    ("specimen", "disease_status_concept_id"),
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head(repo_root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
        ).strip()
    except Exception:
        return None


def _git_status(repo_root: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root, text=True
        )
        return [line for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _concept_integrity(config: EtlConfig) -> dict[str, object]:
    schema = _schema(
        config.raw["sqlserver"].get("target_schema", "dbo"), "target_schema"
    )
    engine = make_engine(config)
    try:
        with engine.connect() as con:
            checks: dict[str, dict[str, object]] = {}
            blockers: list[str] = []
            for table, column, domain, require_standard in AUXILIARY_CONCEPT_CHECKS:
                nonzero = _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}] "
                    f"WHERE COALESCE([{column}],0)<>0",
                )
                bad = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{schema}].[{table}] t
                    LEFT JOIN [{schema}].[concept] c
                      ON c.concept_id=t.[{column}]
                    WHERE COALESCE(t.[{column}],0)<>0
                      AND (
                           c.concept_id IS NULL
                        OR c.invalid_reason IS NOT NULL
                        OR c.domain_id <> '{domain}'
                        {"OR COALESCE(c.standard_concept,'') <> 'S'" if require_standard else ""}
                      )
                    """,
                )
                key = f"{table}.{column}"
                checks[key] = {
                    "nonzero_rows": nonzero,
                    "expected_domain": domain,
                    "require_standard": require_standard,
                    "invalid_rows": bad,
                }
                if bad:
                    blockers.append(f"{key}.invalid_rows={bad}")

            for table, column in ACTIVE_STANDARD_ONLY:
                nonzero = _scalar(
                    con,
                    f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}] "
                    f"WHERE COALESCE([{column}],0)<>0",
                )
                bad = _scalar(
                    con,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{schema}].[{table}] t
                    LEFT JOIN [{schema}].[concept] c
                      ON c.concept_id=t.[{column}]
                    WHERE COALESCE(t.[{column}],0)<>0
                      AND (
                           c.concept_id IS NULL
                        OR c.invalid_reason IS NOT NULL
                        OR COALESCE(c.standard_concept,'') <> 'S'
                      )
                    """,
                )
                key = f"{table}.{column}"
                checks[key] = {
                    "nonzero_rows": nonzero,
                    "expected_domain": None,
                    "require_standard": True,
                    "invalid_rows": bad,
                }
                if bad:
                    blockers.append(f"{key}.invalid_rows={bad}")

            return {"checks": checks, "blockers": blockers}
    finally:
        engine.dispose()


def run_clean_build_phase14_freeze_manifest(config: EtlConfig) -> dict[str, object]:
    review = run_clean_build_phase13_review_decisions(config)
    if review.get("status") != "freeze_candidate_reviewed":
        raise RuntimeError(f"Phase 13 review is not approved: {review.get('status')}")

    visit_time = audit_visit_time_semantics(config)
    if visit_time.get("status") != "matched":
        raise RuntimeError(
            f"Visit-time semantics are not matched: {visit_time.get('status')}"
        )

    integrity = _concept_integrity(config)
    if integrity["blockers"]:
        raise RuntimeError(
            "Auxiliary concept integrity blockers remain: "
            + ", ".join(str(x) for x in integrity["blockers"])
        )

    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parents[2]
    audit_dir = Path(config.audit_dir).resolve()

    source_manifest = {
        str(path.relative_to(repo_root)): _sha256(path)
        for path in sorted(package_dir.glob("*.py"))
    }
    audit_manifest = {
        path.name: _sha256(path)
        for path in sorted(audit_dir.glob("*.json"))
        if path.is_file()
    }

    config_path = getattr(config, "path", None)
    config_checksum = None
    config_display = None
    if config_path:
        p = Path(config_path).resolve()
        if p.exists():
            config_checksum = _sha256(p)
            try:
                config_display = str(p.relative_to(repo_root))
            except ValueError:
                config_display = str(p)

    source_schema = _schema(
        config.raw["sqlserver"].get("source_schema", "dbo"), "source_schema"
    )
    target_schema = _schema(
        config.raw["sqlserver"].get("target_schema", "dbo"), "target_schema"
    )

    payload = {
        "stage": "clean_build_phase14_freeze_manifest",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "freeze_candidate_manifested",
        "database": str(config.raw["sqlserver"].get("database")),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "phase13_status": review.get("status"),
        "unexplained_review_flags": review.get("unexplained_review_flags"),
        "visit_time_semantics_status": visit_time.get("status"),
        "visit_time_interpretations": visit_time.get("interpretations"),
        "auxiliary_concept_integrity": integrity,
        "git_head": _git_head(repo_root),
        "git_status_porcelain": _git_status(repo_root),
        "config_path": config_display,
        "config_sha256": config_checksum,
        "etl_source_sha256": source_manifest,
        "audit_json_sha256": audit_manifest,
        "note": (
            "This manifest records the exact local ETL Python sources and audit JSON files "
            "used for the validated clean rebuild. A dirty working tree is recorded rather "
            "than silently ignored; reproducibility is anchored by per-file SHA-256 hashes."
        ),
    }

    audit_path = config.audit_dir / "clean_build_phase14_freeze_manifest.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the final read-only clean-build freeze-candidate manifest."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase14_freeze_manifest(load_etl_config(args.config))
    print("status:", result["status"])
    print("database:", result["database"])
    print("source_schema:", result["source_schema"])
    print("target_schema:", result["target_schema"])
    print("phase13_status:", result["phase13_status"])
    print("visit_time_semantics_status:", result["visit_time_semantics_status"])
    print("visit_time_interpretations:", result["visit_time_interpretations"])
    print("auxiliary_concept_blockers:", result["auxiliary_concept_integrity"]["blockers"])
    print("git_head:", result["git_head"])
    print("dirty_worktree_entries:", len(result["git_status_porcelain"]))
    print("etl_source_files_hashed:", len(result["etl_source_sha256"]))
    print("audit_json_files_hashed:", len(result["audit_json_sha256"]))
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
