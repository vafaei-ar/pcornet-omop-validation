from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine
from pcornet_omop_validation.study.publication_analysis_manifest import FROZEN_ETL_SHA


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _classified_counts(con, sql: str) -> dict[str, int]:
    return {str(k): int(v) for k, v in con.execute(text(sql)).all()}


def _audit_condition_rows(phase3: dict[str, Any]) -> list[dict[str, Any]]:
    c = phase3.get("condition") or {}
    if not isinstance(c, dict):
        raise RuntimeError("Phase 3 audit does not contain serialized Condition transform results")

    specs = (
        ("DIAGNOSIS", "source_rows", "diagnosis_source_rows"),
        ("DIAGNOSIS", "eligible", "diagnosis_eligible_rows"),
        ("DIAGNOSIS", "missing_id", "diagnosis_missing_id"),
        ("DIAGNOSIS", "missing_patid", "diagnosis_missing_patid"),
        ("DIAGNOSIS", "unlinked_person", "diagnosis_unlinked_person"),
        ("DIAGNOSIS", "missing_dx_date", "diagnosis_missing_dx_date"),
        ("CONDITION", "source_rows", "condition_source_rows"),
        ("CONDITION", "eligible", "condition_eligible_rows"),
        ("CONDITION", "missing_id", "condition_missing_id"),
        ("CONDITION", "missing_patid", "condition_missing_patid"),
        ("CONDITION", "unlinked_person", "condition_unlinked_person"),
        ("CONDITION", "missing_date", "condition_missing_date"),
        ("CONDITION", "invalid_interval", "condition_invalid_interval"),
    )
    out: list[dict[str, Any]] = []
    for family, reason, key in specs:
        if key not in c:
            raise RuntimeError(f"Phase 3 audit missing Condition field: {key}")
        out.append({"source_family": family, "reason": reason, "rows": int(c[key]), "source": "frozen_phase3_audit"})
    return out


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    audit_dir = Path(config.audit_dir).resolve()
    p3 = _load_json(audit_dir / "clean_build_phase3_primary_events.json")
    p8 = _load_json(audit_dir / "clean_build_phase8_drug.json")
    p11 = _load_json(audit_dir / "clean_build_phase11_death.json")
    p14 = _load_json(audit_dir / "clean_build_phase14_freeze_manifest.json")

    if p14.get("git_head") != FROZEN_ETL_SHA:
        raise RuntimeError(
            f"Expected frozen ETL SHA {FROZEN_ETL_SHA}, found {p14.get('git_head')!r}"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))

    procedure_sql = f"""
    WITH classified AS (
      SELECT CASE
        WHEN PROCEDURESID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), PROCEDURESID))) = ''
          THEN 'missing_id'
        WHEN PATID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) = ''
          THEN 'missing_patid'
        WHEN PX_DATE IS NULL THEN 'missing_px_date'
        WHEN PX IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), PX))) = ''
          THEN 'missing_px'
        ELSE 'eligible'
      END AS reason
      FROM [{source_schema}].[PCORnet_PROCEDURES]
    )
    SELECT reason, COUNT_BIG(*) FROM classified GROUP BY reason
    """

    obsclin_sql = f"""
    WITH classified AS (
      SELECT CASE
        WHEN OBSCLINID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLINID))) = ''
          THEN 'missing_id'
        WHEN PATID IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) = ''
          THEN 'missing_patid'
        WHEN OBSCLIN_START_DATE IS NULL THEN 'missing_start_date'
        WHEN OBSCLIN_CODE IS NULL OR LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLIN_CODE))) = ''
          THEN 'missing_code'
        ELSE 'eligible'
      END AS reason
      FROM [{source_schema}].[PCORnet_OBS_CLIN]
    )
    SELECT reason, COUNT_BIG(*) FROM classified GROUP BY reason
    """

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            procedure = _classified_counts(con, procedure_sql)
            obsclin = _classified_counts(con, obsclin_sql)
            procedure_routes = int(
                con.execute(text(
                    f"SELECT COUNT_BIG(DISTINCT source_procedure_id) FROM [{target_schema}].[etl_procedure_event_route]"
                )).scalar_one()
            )
            obsclin_routes = int(
                con.execute(text(
                    f"SELECT COUNT_BIG(DISTINCT source_obsclin_id) FROM [{target_schema}].[etl_obs_clin_route]"
                )).scalar_one()
            )
    finally:
        engine.dispose()

    if procedure.get("eligible", 0) != procedure_routes:
        raise RuntimeError(
            f"Procedure eligibility/route mismatch: eligible={procedure.get('eligible',0):,}, routed={procedure_routes:,}"
        )
    if obsclin.get("eligible", 0) != obsclin_routes:
        raise RuntimeError(
            f"OBS_CLIN eligibility/route mismatch: eligible={obsclin.get('eligible',0):,}, routed={obsclin_routes:,}"
        )

    detail: list[dict[str, Any]] = []
    for family, counts in (("PROCEDURES", procedure), ("OBS_CLIN", obsclin)):
        total = sum(counts.values())
        detail.append({"source_family": family, "reason": "source_rows", "rows": total, "source": "read_only_source_classification"})
        for reason, rows in sorted(counts.items()):
            detail.append({"source_family": family, "reason": reason, "rows": rows, "source": "read_only_source_classification"})

    detail.extend(_audit_condition_rows(p3))

    drug = (p8.get("route_result") or {})
    source_counts = drug.get("source_counts") or {}
    if isinstance(source_counts, dict):
        for family in ("PRESCRIBING", "DISPENSING", "MED_ADMIN", "IMMUNIZATION"):
            if family in source_counts:
                n = int(source_counts[family])
                detail.extend([
                    {"source_family": family, "reason": "source_rows", "rows": n, "source": "frozen_phase8_audit"},
                    {"source_family": family, "reason": "eligible", "rows": n, "source": "frozen_phase8_audit"},
                ])

    death = p11.get("death_result") or {}
    for reason, key in (
        ("source_rows", "source_rows"),
        ("eligible", "eligible_rows"),
        ("missing_patid", "excluded_missing_patid"),
        ("missing_death_date", "excluded_missing_death_date"),
        ("unlinked_person", "excluded_unlinked_person"),
    ):
        if key in death:
            detail.append({"source_family": "DEATH", "reason": reason, "rows": int(death[key]), "source": "frozen_phase11_audit"})

    by_family: dict[str, dict[str, int]] = {}
    for row in detail:
        fam = str(row["source_family"])
        by_family.setdefault(fam, {})[str(row["reason"])] = int(row["rows"])

    summary: list[dict[str, Any]] = []
    for family, counts in by_family.items():
        source_rows = counts.get("source_rows")
        eligible = counts.get("eligible")
        if source_rows is None or eligible is None:
            continue
        excluded = source_rows - eligible
        reasons = {k: v for k, v in counts.items() if k not in {"source_rows", "eligible"} and v}
        if sum(reasons.values()) != excluded:
            raise RuntimeError(
                f"Exclusion decomposition does not reconcile for {family}: source={source_rows}, eligible={eligible}, reasons={reasons}"
            )
        summary.append({
            "source_family": family,
            "source_rows": source_rows,
            "eligible_rows": eligible,
            "excluded_rows": excluded,
            "excluded_percent": (excluded / source_rows * 100.0) if source_rows else None,
            "exclusion_reasons": "; ".join(f"{k}={v}" for k, v in reasons.items()) or "none",
        })

    result_root = Path(output_dir).resolve() if output_dir else audit_dir.parent / "publication_analysis" / "stage_a"
    result_root.mkdir(parents=True, exist_ok=True)
    _write_csv(result_root / "source_exclusion_reasons.csv", detail, ["source_family", "reason", "rows", "source"])
    _write_csv(result_root / "source_eligibility_exclusions_detailed.csv", summary, ["source_family", "source_rows", "eligible_rows", "excluded_rows", "excluded_percent", "exclusion_reasons"])

    repo_root = Path(__file__).resolve().parents[3]
    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    worktree = _git(repo_root, "status", "--porcelain") or ""
    payload = {
        "stage": "stage_a_exclusion_decomposition",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "stage_a_exclusion_decomposition_complete",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_worktree_clean": not bool(worktree.strip()),
        "classification_policy": (
            "Procedure and OBS_CLIN exclusions are mutually exclusive by the CASE precedence encoded here, "
            "using the same required-field predicates as the frozen route builders. Condition/Diagnosis and Death "
            "reason counts are taken from the frozen materialization audits. Drug-source families had complete, "
            "unique source identifiers as required by the frozen Drug route builder and therefore have no source-level exclusions."
        ),
        "summary": summary,
    }
    json_path = result_root / "stage_a_exclusion_decomposition.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    md = [
        "# Stage A source eligibility and exclusion decomposition",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "| Source family | Source rows | Eligible rows | Excluded rows | Excluded % | Reasons |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary:
        md.append(
            f"| {row['source_family']} | {row['source_rows']:,} | {row['eligible_rows']:,} | "
            f"{row['excluded_rows']:,} | {row['excluded_percent']:.3f}% | {row['exclusion_reasons']} |"
        )
    md.extend([
        "",
        "Procedure and OBS_CLIN categories are mutually exclusive under an explicit precedence solely for reporting. "
        "Eligibility itself is unchanged from the frozen ETL predicates. Counts are descriptive outputs, not acceptance thresholds.",
        "",
    ])
    (result_root / "stage_a_exclusion_summary.md").write_text("\n".join(md), encoding="utf-8")

    return {
        "status": payload["status"],
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_worktree_clean": payload["analysis_worktree_clean"],
        "source_families": len(summary),
        "detail_rows": len(detail),
        "output_dir": str(result_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decompose Stage A source eligibility and exclusions by explicit reason.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    result = run(args.config, args.output_dir)
    for key in ("status", "frozen_etl_sha", "analysis_git_sha", "analysis_worktree_clean", "source_families", "detail_rows", "output_dir"):
        print(f"{key}: {result[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
