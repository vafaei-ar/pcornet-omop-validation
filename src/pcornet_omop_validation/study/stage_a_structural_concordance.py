from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcornet_omop_validation.etl.config import load_etl_config


FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"

REQUIRED_AUDITS = (
    "clean_build_phase1.json",
    "clean_build_phase2_routes.json",
    "clean_build_phase3_primary_events.json",
    "clean_build_phase4_measurement_base.json",
    "clean_build_phase5_measurement_obsclin.json",
    "clean_build_phase6_observation.json",
    "clean_build_phase7_condition_obsclin.json",
    "clean_build_phase8_drug.json",
    "clean_build_phase9_procedure_remaining.json",
    "clean_build_phase10_condition_cross_domain.json",
    "clean_build_phase11_death.json",
    "clean_build_phase12_validation.json",
    "clean_build_phase13_review_decisions.json",
    "clean_build_phase14_freeze_manifest.json",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _get(payload: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _first(payload: dict[str, Any], paths: tuple[tuple[str, ...], ...], default: Any = None) -> Any:
    for path in paths:
        value = _get(payload, *path, default=None)
        if value is not None:
            return value
    return default


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _validate_freeze(audits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    p12 = audits["clean_build_phase12_validation.json"]
    p13 = audits["clean_build_phase13_review_decisions.json"]
    p14 = audits["clean_build_phase14_freeze_manifest.json"]

    reconciliation_status = _first(
        p12,
        (("global_reconciliation_status",), ("global_reconciliation", "status")),
    )
    if reconciliation_status != "matched":
        raise RuntimeError(
            f"Stage A requires matched Phase 12 global reconciliation; found {reconciliation_status!r}"
        )

    hard_blockers = _first(
        p12,
        (("semantic_hard_blockers",), ("semantic_freeze", "hard_blockers")),
        default=[],
    )
    if hard_blockers:
        raise RuntimeError(f"Stage A requires zero semantic hard blockers: {hard_blockers}")

    unexplained = _get(p13, "unexplained_review_flags", default=[])
    if unexplained:
        raise RuntimeError(f"Stage A requires zero unexplained review flags: {unexplained}")

    auxiliary = _first(
        p14,
        (("auxiliary_concept_integrity", "blockers"), ("auxiliary_concept_blockers",)),
        default=[],
    )
    if auxiliary:
        raise RuntimeError(f"Stage A requires zero auxiliary concept blockers: {auxiliary}")

    freeze_sha = _get(p14, "git_head")
    if freeze_sha != FROZEN_ETL_SHA:
        raise RuntimeError(
            "Phase 14 does not identify the publication ETL freeze: "
            f"expected {FROZEN_ETL_SHA}, found {freeze_sha!r}"
        )

    freeze_status = _get(p14, "git_status_porcelain", default=[])
    if freeze_status:
        raise RuntimeError(
            "Phase 14 freeze manifest was produced from a dirty ETL worktree: "
            f"{freeze_status}"
        )

    return {
        "global_reconciliation_status": reconciliation_status,
        "semantic_hard_blockers": hard_blockers,
        "unexplained_review_flags": unexplained,
        "auxiliary_concept_blockers": auxiliary,
        "frozen_etl_sha": freeze_sha,
        "freeze_worktree_clean": not freeze_status,
        "visit_time_semantics_status": _get(p14, "visit_time_semantics_status"),
    }


def _final_target_rows(p12: dict[str, Any]) -> dict[str, int]:
    rows = _get(p12, "target_rows", default={})
    if not isinstance(rows, dict) or not rows:
        rows = _get(p12, "global_reconciliation", "target_rows", default={})
    return {str(k): int(v) for k, v in rows.items() if _as_int(v) is not None}


def _concept_zero_rows(audits: dict[str, dict[str, Any]], target_rows: dict[str, int]) -> list[dict[str, Any]]:
    p3 = audits["clean_build_phase3_primary_events.json"]
    p4 = audits["clean_build_phase4_measurement_base.json"]
    p6 = audits["clean_build_phase6_observation.json"]
    p8 = audits["clean_build_phase8_drug.json"]
    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    p11 = audits["clean_build_phase11_death.json"]
    p13 = audits["clean_build_phase13_review_decisions.json"]

    type_zero = _get(p13, "type_zero_rows", default={})
    rows: list[dict[str, Any]] = []

    def add(domain: str, metric: str, count: Any, denominator: int | None, reason: str) -> None:
        n = _as_int(count)
        if n is None:
            return
        rows.append(
            {
                "domain": domain,
                "metric": metric,
                "count": n,
                "denominator": denominator,
                "rate": _rate(n, denominator),
                "interpretation": reason,
            }
        )

    add(
        "condition_occurrence",
        "condition_concept_id_0",
        _first(p3, (("condition_concept_zero",), ("condition_result", "concept_zero_rows"))),
        target_rows.get("condition_occurrence"),
        "Unresolved canonical source semantics are retained explicitly rather than imputed.",
    )
    add(
        "procedure_occurrence",
        "procedure_concept_id_0",
        _first(p3, (("procedure_concept_zero",), ("procedure_result", "concept_zero_rows"))),
        target_rows.get("procedure_occurrence"),
        "Unresolved Procedure routes remain concept 0.",
    )
    add(
        "measurement",
        "measurement_concept_id_0_base",
        _first(p4, (("target_concept_zero_rows",), ("measurement_result", "concept_zero_rows"))),
        target_rows.get("measurement"),
        "Base Measurement unresolved concept coverage.",
    )
    add(
        "observation",
        "observation_concept_id_0",
        _first(p6, (("concept_zero_rows",), ("observation_result", "concept_zero_rows"))),
        target_rows.get("observation"),
        "Observation unresolved concept coverage.",
    )
    add(
        "drug_exposure",
        "drug_concept_id_0",
        _get(p8, "drug_result", "concept_zero_rows"),
        target_rows.get("drug_exposure"),
        "Drug source code could not be uniquely resolved to an active Standard drug concept.",
    )
    add(
        "drug_exposure",
        "nonblank_route_source_route_concept_id_0",
        _get(p13, "drug_nonblank_route_zero_rows"),
        target_rows.get("drug_exposure"),
        "Nonblank standardized route source remained unresolved under unique exact Route mapping policy.",
    )
    add(
        "death",
        "death_type_concept_id_0",
        _first(p11, (("death_type_concept_zero_rows",), ("death_result", "death_type_concept_zero_rows"))),
        target_rows.get("death"),
        "Explicit provenance policy; no defensible source-derived death type semantics.",
    )

    if isinstance(type_zero, dict):
        for domain, count in type_zero.items():
            add(
                str(domain),
                "type_concept_id_0",
                count,
                target_rows.get(str(domain)),
                "Explicit reviewed type-concept provenance gap; recorded rather than imputed.",
            )

    procedure_remaining = _get(p9, "concept_zero_rows", default={})
    if isinstance(procedure_remaining, dict):
        for domain, count in procedure_remaining.items():
            target_name = {
                "Condition": "condition_occurrence",
                "Device": "device_exposure",
                "Specimen": "specimen",
            }.get(str(domain), str(domain))
            add(
                target_name,
                "procedure_remaining_concept_id_0",
                count,
                target_rows.get(target_name),
                "Procedure-routed remaining-domain unresolved concept coverage.",
            )

    return rows


def _reconciliation_rows(audits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    p1 = audits["clean_build_phase1.json"]
    after = _get(p1, "after_target_rows", default={})
    if isinstance(after, dict):
        for domain, count in after.items():
            rows.append({"stage": "phase1", "component": str(domain), "route_or_expected_rows": count, "target_rows": count, "lineage_rows": None, "status": "matched"})

    p3 = audits["clean_build_phase3_primary_events.json"]
    rec = _get(p3, "reconciliation", default={})
    if isinstance(rec, dict):
        rows.extend(
            [
                {"stage": "phase3", "component": "condition_primary", "route_or_expected_rows": rec.get("condition_routes"), "target_rows": rec.get("condition_occurrence"), "lineage_rows": rec.get("condition_xwalk"), "status": _get(p3, "condition_status")},
                {"stage": "phase3", "component": "procedure_primary", "route_or_expected_rows": rec.get("procedure_routes"), "target_rows": rec.get("procedure_occurrence"), "lineage_rows": rec.get("procedure_xwalk"), "status": _get(p3, "procedure_status")},
            ]
        )

    for fname, stage, component, expected_paths, target_paths, lineage_paths, status_paths in (
        ("clean_build_phase4_measurement_base.json", "phase4", "measurement_base", (("expected_rows",), ("measurement_result", "expected_rows")), (("target_rows",), ("measurement_result", "target_rows")), (("lineage_rows",), ("measurement_result", "lineage_rows")), (("measurement_status",), ("measurement_result", "status"))),
        ("clean_build_phase5_measurement_obsclin.json", "phase5", "measurement_obs_clin_append", (("obs_clin_route_rows",),), (("measurement_rows",),), (("measurement_xwalk_rows",),), (("append_status",),)),
        ("clean_build_phase6_observation.json", "phase6", "observation", (("expected_rows",),), (("target_rows",),), (("lineage_rows",),), (("status",),)),
        ("clean_build_phase7_condition_obsclin.json", "phase7", "condition_obs_clin_append", (("obs_clin_condition_rows",),), (("condition_occurrence_rows",),), (("obs_clin_condition_xwalk_rows",),), (("append_status",),)),
        ("clean_build_phase8_drug.json", "phase8", "drug_exposure", (("drug_result", "eligible_route_rows"),), (("post_counts", "drug_exposure_rows"),), (("post_counts", "drug_exposure_xwalk_rows"),), (("drug_result", "status"),)),
        ("clean_build_phase11_death.json", "phase11", "death", (("death_result", "eligible_rows"), ("eligible_rows",)), (("post_counts", "death_rows"), ("death_rows",)), (("post_counts", "death_xwalk_rows"), ("death_xwalk_rows",)), (("death_result", "status"), ("death_status",))),
    ):
        p = audits[fname]
        rows.append(
            {
                "stage": stage,
                "component": component,
                "route_or_expected_rows": _first(p, expected_paths),
                "target_rows": _first(p, target_paths),
                "lineage_rows": _first(p, lineage_paths),
                "status": _first(p, status_paths),
            }
        )

    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    routes = _get(p9, "route_rows", default={})
    xwalks = {
        "Condition": _get(p9, "procedure_condition_xwalk_rows"),
        "Device": _get(p9, "device_xwalk_rows"),
        "Specimen": _get(p9, "specimen_xwalk_rows"),
    }
    targets = {
        "Condition": _get(p9, "route_rows", "Condition"),
        "Device": _get(p9, "device_exposure_rows"),
        "Specimen": _get(p9, "specimen_rows"),
    }
    if isinstance(routes, dict):
        for domain, count in routes.items():
            rows.append({"stage": "phase9", "component": f"procedure_to_{str(domain).lower()}", "route_or_expected_rows": count, "target_rows": targets.get(str(domain)), "lineage_rows": xwalks.get(str(domain)), "status": _get(p9, "materialization_status")})

    p10 = audits["clean_build_phase10_condition_cross_domain.json"]
    route_rows = _get(p10, "route_rows", default={})
    xwalk_rows = _get(p10, "cross_domain_xwalk_rows", default={})
    if isinstance(route_rows, dict):
        for domain, count in route_rows.items():
            rows.append({"stage": "phase10", "component": f"condition_to_{str(domain).lower()}", "route_or_expected_rows": count, "target_rows": None, "lineage_rows": xwalk_rows.get(domain) if isinstance(xwalk_rows, dict) else None, "status": _get(p10, "materialization_status")})

    return rows


def _cross_domain_rows(audits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    p10 = audits["clean_build_phase10_condition_cross_domain.json"]

    routes9 = _get(p9, "route_rows", default={})
    if isinstance(routes9, dict):
        for target_domain, count in routes9.items():
            rows.append({"source_family": "PROCEDURES", "target_domain": target_domain, "route_rows": count, "xwalk_rows": _get(p9, {"Condition": "procedure_condition_xwalk_rows", "Device": "device_xwalk_rows", "Specimen": "specimen_xwalk_rows"}.get(str(target_domain), ""))})

    routes10 = _get(p10, "route_rows", default={})
    xwalk10 = _get(p10, "cross_domain_xwalk_rows", default={})
    if isinstance(routes10, dict):
        for target_domain, count in routes10.items():
            rows.append({"source_family": "DIAGNOSIS/CONDITION", "target_domain": target_domain, "route_rows": count, "xwalk_rows": xwalk10.get(target_domain) if isinstance(xwalk10, dict) else None})
    return rows


def _coverage_rows(audits: dict[str, dict[str, Any]], target_rows: dict[str, int]) -> list[dict[str, Any]]:
    p4 = audits["clean_build_phase4_measurement_base.json"]
    p5 = audits["clean_build_phase5_measurement_obsclin.json"]
    p6 = audits["clean_build_phase6_observation.json"]
    p7 = audits["clean_build_phase7_condition_obsclin.json"]
    p8 = audits["clean_build_phase8_drug.json"]

    values = [
        ("measurement_lab_unit", _first(p4, (("lab_unit_concept_zero_rows",),)), _first(p4, (("lab_measurement_rows",),)), "zero_count"),
        ("measurement_obsclin_unit", _first(p5, (("obs_clin_unit_zero_rows",),)), _first(p5, (("obs_clin_route_rows",),)), "zero_count"),
        ("observation_visit_linkage", _first(p6, (("visit_linked_rows",),)), target_rows.get("observation"), "linked_count"),
        ("condition_obsclin_visit_linkage", _first(p7, (("visit_linked_rows",),)), _first(p7, (("obs_clin_condition_rows",),)), "linked_count"),
        ("drug_visit_linkage", _get(p8, "drug_result", "visit_linked_rows"), target_rows.get("drug_exposure"), "linked_count"),
        ("drug_route_mapped", _get(p8, "route_finalize_result", "mapped_rows"), _get(p8, "route_finalize_result", "standardized_route_rows"), "mapped_count"),
    ]
    rows: list[dict[str, Any]] = []
    for metric, numerator, denominator, mode in values:
        n = _as_int(numerator)
        d = _as_int(denominator)
        rows.append({"metric": metric, "numerator": n, "denominator": d, "rate": _rate(n, d), "mode": mode})
    return rows


def _route_summary(audits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    p2 = audits["clean_build_phase2_routes.json"]
    c = _get(p2, "condition_canonical_summary", default={})
    return {
        "route_ledger_rows": _get(p2, "route_ledger_rows", default={}),
        "condition_source_events": _get(c, "source_events") if isinstance(c, dict) else None,
        "condition_route_rows": _get(c, "route_rows") if isinstance(c, dict) else None,
        "condition_core_event_route_rows": _get(c, "core_event_route_rows") if isinstance(c, dict) else None,
        "condition_non_event_standard_route_rows": _get(c, "non_event_standard_route_rows") if isinstance(c, dict) else None,
        "condition_fallback_condition_zero_rows": _get(c, "fallback_condition_zero_rows") if isinstance(c, dict) else None,
        "condition_multi_core_route_source_events": _get(c, "multi_core_route_source_events") if isinstance(c, dict) else None,
    }


def run_stage_a(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    audit_dir = Path(config.audit_dir).expanduser().resolve()
    missing = [name for name in REQUIRED_AUDITS if not (audit_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Stage A requires the final clean-build Phase 1-14 audit bundle; missing: "
            + ", ".join(missing)
        )

    audits = {name: _load_json(audit_dir / name) for name in REQUIRED_AUDITS}
    freeze_validation = _validate_freeze(audits)
    p12 = audits["clean_build_phase12_validation.json"]
    target_rows = _final_target_rows(p12)
    if not target_rows:
        raise RuntimeError("Could not recover final target row counts from Phase 12 audit")

    result_root = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else (audit_dir.parent / "publication_analysis" / "stage_a")
    )
    result_root.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    analysis_sha = _git(repo_root, "rev-parse", "HEAD")
    analysis_branch = _git(repo_root, "branch", "--show-current")
    worktree = _git(repo_root, "status", "--porcelain") or ""
    worktree_entries = [line for line in worktree.splitlines() if line.strip()]

    target_table_rows = [
        {"omop_table": table, "rows": rows}
        for table, rows in target_rows.items()
    ]
    concept_zero = _concept_zero_rows(audits, target_rows)
    reconciliation = _reconciliation_rows(audits)
    cross_domain = _cross_domain_rows(audits)
    coverage = _coverage_rows(audits, target_rows)
    route_summary = _route_summary(audits)

    audit_hashes = {name: _sha256(audit_dir / name) for name in REQUIRED_AUDITS}
    generated_at = datetime.now(timezone.utc)
    payload = {
        "stage": "stage_a_structural_semantic_concordance",
        "recorded_at_utc": generated_at.isoformat(),
        "status": "stage_a_aggregate_summary_complete",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_git_branch": analysis_branch,
        "analysis_worktree_clean": not worktree_entries,
        "analysis_git_status_porcelain": worktree_entries,
        "database": str(config.raw["sqlserver"].get("database")),
        "source_schema": str(config.raw["sqlserver"].get("source_schema", "dbo")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "freeze_validation": freeze_validation,
        "input_audit_sha256": audit_hashes,
        "final_target_rows": target_rows,
        "route_summary": route_summary,
        "reconciliation": reconciliation,
        "concept_zero_summary": concept_zero,
        "coverage_summary": coverage,
        "cross_domain_routes": cross_domain,
        "interpretation": (
            "Stage A is a read-only synthesis of the frozen ETL audit bundle. Row counts are reported as outcomes, "
            "not acceptance thresholds, and this analysis does not modify ETL mappings or query the comparator database."
        ),
    }

    json_path = result_root / "stage_a_summary.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_csv(result_root / "final_target_counts.csv", target_table_rows, ["omop_table", "rows"])
    _write_csv(result_root / "reconciliation_summary.csv", reconciliation, ["stage", "component", "route_or_expected_rows", "target_rows", "lineage_rows", "status"])
    _write_csv(result_root / "concept_zero_summary.csv", concept_zero, ["domain", "metric", "count", "denominator", "rate", "interpretation"])
    _write_csv(result_root / "coverage_summary.csv", coverage, ["metric", "numerator", "denominator", "rate", "mode"])
    _write_csv(result_root / "cross_domain_routes.csv", cross_domain, ["source_family", "target_domain", "route_rows", "xwalk_rows"])

    md = [
        "# Stage A structural and semantic concordance",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        f"Analysis code SHA: `{analysis_sha}`",
        "",
        "## Freeze gates",
        "",
        f"- Global reconciliation: **{freeze_validation['global_reconciliation_status']}**",
        f"- Semantic hard blockers: **{len(freeze_validation['semantic_hard_blockers'])}**",
        f"- Unexplained review flags: **{len(freeze_validation['unexplained_review_flags'])}**",
        f"- Auxiliary concept blockers: **{len(freeze_validation['auxiliary_concept_blockers'])}**",
        f"- Visit-time semantics: **{freeze_validation['visit_time_semantics_status']}**",
        "",
        "## Final OMOP target counts",
        "",
        "| OMOP table | Rows |",
        "| --- | ---: |",
    ]
    for row in target_table_rows:
        md.append(f"| {row['omop_table']} | {int(row['rows']):,} |")
    md.extend(
        [
            "",
            "## Interpretation",
            "",
            "These are aggregate outputs of the frozen ETL and its recorded source/vocabulary semantics. They are not hard-coded acceptance thresholds. Stage A does not retune ETL mappings and does not use the historical comparator database as an acceptance target.",
            "",
            "Machine-readable tables in this directory provide reconciliation, concept-0, coverage, and cross-domain routing summaries for manuscript development.",
        ]
    )
    md_path = result_root / "stage_a_summary.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    return {
        "status": payload["status"],
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": analysis_sha,
        "analysis_worktree_clean": not worktree_entries,
        "output_dir": str(result_root),
        "summary_json": str(json_path),
        "summary_markdown": str(md_path),
        "target_table_count": len(target_table_rows),
        "reconciliation_rows": len(reconciliation),
        "concept_zero_rows": len(concept_zero),
        "coverage_rows": len(coverage),
        "cross_domain_rows": len(cross_domain),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Stage A structural/semantic concordance aggregates from the frozen ETL audit bundle."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    result = run_stage_a(args.config, output_dir=args.output_dir)
    print("status:", result["status"])
    print("frozen_etl_sha:", result["frozen_etl_sha"])
    print("analysis_git_sha:", result["analysis_git_sha"])
    print("analysis_worktree_clean:", result["analysis_worktree_clean"])
    print("target_table_count:", result["target_table_count"])
    print("reconciliation_rows:", result["reconciliation_rows"])
    print("concept_zero_rows:", result["concept_zero_rows"])
    print("coverage_rows:", result["coverage_rows"])
    print("cross_domain_rows:", result["cross_domain_rows"])
    print("output_dir:", result["output_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
