from __future__ import annotations

import argparse
from typing import Any

from . import stage_a_structural_concordance as base


def _concept_zero_rows(
    audits: dict[str, dict[str, Any]], target_rows: dict[str, int]
) -> list[dict[str, Any]]:
    p3 = audits["clean_build_phase3_primary_events.json"]
    p4 = audits["clean_build_phase4_measurement_base.json"]
    p6 = audits["clean_build_phase6_observation.json"]
    p8 = audits["clean_build_phase8_drug.json"]
    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    p11 = audits["clean_build_phase11_death.json"]
    p13 = audits["clean_build_phase13_review_decisions.json"]

    rows: list[dict[str, Any]] = []

    def add(domain: str, metric: str, count: Any, denominator: int | None, reason: str) -> None:
        n = base._as_int(count)
        if n is None:
            return
        rows.append(
            {
                "domain": domain,
                "metric": metric,
                "count": n,
                "denominator": denominator,
                "rate": base._rate(n, denominator),
                "interpretation": reason,
            }
        )

    diagnosis_zero = base._as_int(base._get(p3, "condition", "diagnosis_concept_zero")) or 0
    condition_zero = base._as_int(base._get(p3, "condition", "condition_concept_zero")) or 0
    add(
        "condition_occurrence",
        "condition_concept_id_0_primary",
        diagnosis_zero + condition_zero,
        target_rows.get("condition_occurrence"),
        "Unresolved canonical DIAGNOSIS/CONDITION semantics are retained explicitly rather than imputed.",
    )
    add(
        "procedure_occurrence",
        "procedure_concept_id_0_primary",
        base._get(p3, "procedure", "concept_zero_rows"),
        target_rows.get("procedure_occurrence"),
        "Unresolved Procedure routes remain concept 0.",
    )
    add(
        "measurement",
        "measurement_concept_id_0_base",
        base._get(p4, "measurement_result", "target_concept_zero_rows"),
        target_rows.get("measurement"),
        "Base Measurement unresolved concept coverage.",
    )
    add(
        "observation",
        "observation_concept_id_0",
        base._get(p6, "observation_result", "concept_zero_rows"),
        target_rows.get("observation"),
        "Observation unresolved concept coverage.",
    )
    add(
        "drug_exposure",
        "drug_concept_id_0",
        base._get(p8, "drug_result", "concept_zero_rows"),
        target_rows.get("drug_exposure"),
        "Drug source code could not be uniquely resolved to an active Standard drug concept.",
    )
    add(
        "drug_exposure",
        "nonblank_route_source_route_concept_id_0",
        base._get(p13, "drug_nonblank_route_zero_rows"),
        target_rows.get("drug_exposure"),
        "Nonblank standardized route source remained unresolved under unique exact Route mapping policy.",
    )
    add(
        "death",
        "death_type_concept_id_0",
        base._get(p11, "death_result", "death_type_concept_zero_rows"),
        target_rows.get("death"),
        "Explicit provenance policy; no defensible source-derived death type semantics.",
    )

    type_zero = base._get(p13, "type_zero_rows", default={})
    if isinstance(type_zero, dict):
        for domain, count in type_zero.items():
            add(
                str(domain),
                "type_concept_id_0",
                count,
                target_rows.get(str(domain)),
                "Explicit reviewed type-concept provenance gap; recorded rather than imputed.",
            )

    procedure_remaining = base._get(p9, "materialization", "concept_zero_rows", default={})
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
    after = base._get(p1, "after_target_rows", default={})
    if isinstance(after, dict):
        for domain, count in after.items():
            rows.append(
                {
                    "stage": "phase1",
                    "component": str(domain),
                    "route_or_expected_rows": count,
                    "target_rows": count,
                    "lineage_rows": None,
                    "status": base._get(p1, str(domain), "status"),
                }
            )

    p3 = audits["clean_build_phase3_primary_events.json"]
    rec = base._get(p3, "reconciliation", default={})
    if isinstance(rec, dict):
        rows.extend(
            [
                {
                    "stage": "phase3",
                    "component": "condition_primary",
                    "route_or_expected_rows": rec.get("condition_routes"),
                    "target_rows": rec.get("condition_occurrence"),
                    "lineage_rows": rec.get("condition_xwalk"),
                    "status": base._get(p3, "condition", "status"),
                },
                {
                    "stage": "phase3",
                    "component": "procedure_primary",
                    "route_or_expected_rows": rec.get("procedure_routes"),
                    "target_rows": rec.get("procedure_occurrence"),
                    "lineage_rows": rec.get("procedure_xwalk"),
                    "status": base._get(p3, "procedure", "status"),
                },
            ]
        )

    p4 = audits["clean_build_phase4_measurement_base.json"]
    m4 = base._get(p4, "measurement_result", default={})
    rows.append({"stage": "phase4", "component": "measurement_base", "route_or_expected_rows": m4.get("expected_rows"), "target_rows": m4.get("target_rows"), "lineage_rows": m4.get("lineage_rows"), "status": m4.get("status")})

    p5 = audits["clean_build_phase5_measurement_obsclin.json"]
    rows.append({"stage": "phase5", "component": "measurement_obs_clin_append", "route_or_expected_rows": base._get(p5, "post_counts", "obs_clin_routes"), "target_rows": base._get(p5, "post_counts", "measurement"), "lineage_rows": base._get(p5, "post_counts", "measurement_xwalk"), "status": base._get(p5, "append_result", "status")})

    p6 = audits["clean_build_phase6_observation.json"]
    o6 = base._get(p6, "observation_result", default={})
    rows.append({"stage": "phase6", "component": "observation", "route_or_expected_rows": o6.get("expected_rows"), "target_rows": o6.get("target_rows"), "lineage_rows": o6.get("lineage_rows"), "status": o6.get("status")})

    p7 = audits["clean_build_phase7_condition_obsclin.json"]
    rows.append({"stage": "phase7", "component": "condition_obs_clin_append", "route_or_expected_rows": base._get(p7, "append_result", "obs_clin_condition_rows"), "target_rows": base._get(p7, "post_counts", "condition_occurrence"), "lineage_rows": base._get(p7, "post_counts", "obs_clin_condition_xwalk"), "status": base._get(p7, "append_result", "status")})

    p8 = audits["clean_build_phase8_drug.json"]
    rows.append({"stage": "phase8", "component": "drug_exposure", "route_or_expected_rows": base._get(p8, "drug_result", "eligible_route_rows"), "target_rows": base._get(p8, "post_counts", "drug_exposure_rows"), "lineage_rows": base._get(p8, "post_counts", "drug_exposure_xwalk_rows"), "status": base._get(p8, "drug_result", "status")})

    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    routes9 = base._get(p9, "materialization", "route_rows", default={})
    if isinstance(routes9, dict):
        for domain, count in routes9.items():
            lineage_key = {"Condition": "procedure_condition_xwalk", "Device": "device_xwalk", "Specimen": "specimen_xwalk"}.get(str(domain))
            target_key = {"Condition": "condition_occurrence", "Device": "device_exposure", "Specimen": "specimen"}.get(str(domain))
            rows.append({"stage": "phase9", "component": f"procedure_to_{str(domain).lower()}", "route_or_expected_rows": count, "target_rows": base._get(p9, "post_counts", target_key) if target_key else None, "lineage_rows": base._get(p9, "post_counts", lineage_key) if lineage_key else None, "status": base._get(p9, "materialization", "status")})

    p10 = audits["clean_build_phase10_condition_cross_domain.json"]
    routes10 = base._get(p10, "post_counts", "route_rows", default={})
    xwalk10 = base._get(p10, "post_counts", "cross_domain_xwalk_rows", default={})
    targets10 = base._get(p10, "post_counts", "target_rows", default={})
    if isinstance(routes10, dict):
        for domain, count in routes10.items():
            rows.append({"stage": "phase10", "component": f"condition_to_{str(domain).lower()}", "route_or_expected_rows": count, "target_rows": targets10.get(domain) if isinstance(targets10, dict) else None, "lineage_rows": xwalk10.get(domain) if isinstance(xwalk10, dict) else None, "status": base._get(p10, "materialization_result", "status")})

    p11 = audits["clean_build_phase11_death.json"]
    rows.append({"stage": "phase11", "component": "death", "route_or_expected_rows": base._get(p11, "death_result", "eligible_rows"), "target_rows": base._get(p11, "post_counts", "death_rows"), "lineage_rows": base._get(p11, "post_counts", "death_xwalk_rows"), "status": base._get(p11, "death_result", "status")})

    return rows


def _cross_domain_rows(audits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p9 = audits["clean_build_phase9_procedure_remaining.json"]
    p10 = audits["clean_build_phase10_condition_cross_domain.json"]

    routes9 = base._get(p9, "materialization", "route_rows", default={})
    if isinstance(routes9, dict):
        for target_domain, count in routes9.items():
            xwalk_key = {
                "Condition": "procedure_condition_xwalk",
                "Device": "device_xwalk",
                "Specimen": "specimen_xwalk",
            }.get(str(target_domain))
            rows.append(
                {
                    "source_family": "PROCEDURES",
                    "target_domain": target_domain,
                    "route_rows": count,
                    "xwalk_rows": base._get(p9, "post_counts", xwalk_key) if xwalk_key else None,
                }
            )

    routes10 = base._get(p10, "post_counts", "route_rows", default={})
    xwalk10 = base._get(p10, "post_counts", "cross_domain_xwalk_rows", default={})
    if isinstance(routes10, dict):
        for target_domain, count in routes10.items():
            rows.append(
                {
                    "source_family": "DIAGNOSIS/CONDITION",
                    "target_domain": target_domain,
                    "route_rows": count,
                    "xwalk_rows": xwalk10.get(target_domain) if isinstance(xwalk10, dict) else None,
                }
            )
    return rows


def _coverage_rows(
    audits: dict[str, dict[str, Any]], target_rows: dict[str, int]
) -> list[dict[str, Any]]:
    p4 = audits["clean_build_phase4_measurement_base.json"]
    p5 = audits["clean_build_phase5_measurement_obsclin.json"]
    p6 = audits["clean_build_phase6_observation.json"]
    p7 = audits["clean_build_phase7_condition_obsclin.json"]
    p8 = audits["clean_build_phase8_drug.json"]

    values = [
        ("measurement_lab_unit", base._get(p4, "measurement_result", "lab_unit_concept_zero_rows"), base._get(p4, "measurement_result", "lab_measurement_rows"), "zero_count"),
        ("measurement_obsclin_unit", base._get(p5, "post_counts", "obs_clin_unit_zero"), base._get(p5, "post_counts", "obs_clin_routes"), "zero_count"),
        ("observation_visit_linkage", base._get(p6, "observation_result", "visit_linked_rows"), target_rows.get("observation"), "linked_count"),
        ("condition_obsclin_visit_linkage", base._get(p7, "append_result", "visit_linked_rows"), base._get(p7, "append_result", "obs_clin_condition_rows"), "linked_count"),
        ("drug_visit_linkage", base._get(p8, "drug_result", "visit_linked_rows"), target_rows.get("drug_exposure"), "linked_count"),
        ("drug_route_mapped", base._get(p8, "route_finalize_result", "mapped_rows"), base._get(p8, "route_finalize_result", "standardized_route_rows"), "mapped_count"),
    ]
    rows: list[dict[str, Any]] = []
    for metric, numerator, denominator, mode in values:
        n = base._as_int(numerator)
        d = base._as_int(denominator)
        rows.append({"metric": metric, "numerator": n, "denominator": d, "rate": base._rate(n, d), "mode": mode})
    return rows


def run_stage_a(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    # Patch only the audit-schema extraction helpers. The base module retains all
    # freeze validation, hashing, output writing, and provenance behavior.
    base._concept_zero_rows = _concept_zero_rows
    base._reconciliation_rows = _reconciliation_rows
    base._cross_domain_rows = _cross_domain_rows
    base._coverage_rows = _coverage_rows
    return base.run_stage_a(config_path, output_dir=output_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build corrected Stage A structural/semantic concordance aggregates from the frozen ETL audit bundle."
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
