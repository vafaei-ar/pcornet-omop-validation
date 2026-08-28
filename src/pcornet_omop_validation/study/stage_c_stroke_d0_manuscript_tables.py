from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = "stage-c-stroke-d0-v1"


def _load(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"Missing required Stage C D0 output: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError(f"Output is not anchored to frozen ETL SHA: {path}")
    if payload.get("study_definition") != STUDY_DEFINITION:
        raise RuntimeError(f"Unexpected study definition in {path}: {payload.get('study_definition')}")
    return payload


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def run(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    root = config.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes" / "stroke_d0"

    preflight = _load(root / "stage_c_stroke_d0_preflight.json")
    result = _load(root / "stage_c_stroke_d0_concordance.json")

    primary = result["primary_transformation_fidelity"]
    portable = result["secondary_native_omop_portability"]
    source = result["source_reference"]
    categories = result["primary_source_only_discordance_categories"]

    discordance_total = sum(int(r["patients"]) for r in categories)
    required_date_excluded = sum(
        int(r["patients"])
        for r in categories
        if r["category"] == "required_source_date_missing_or_etl_excluded"
    )

    checks = {
        "preflight_did_not_query_outcomes": preflight.get("outcome_query_performed") is False,
        "patient_bridge_matched_unique": preflight["patient_bridge"]["status"] == "matched_unique_source_bridge",
        "native_pdx_nonrepresentability_confirmed": preflight["native_omop_representability"]["pdx_natively_representable"] is False,
        "source_partition_closes": primary["intersection_patients"] + primary["source_only_patients"] == primary["source_patients"],
        "omop_partition_closes": primary["intersection_patients"] + primary["omop_only_patients"] == primary["omop_patients"],
        "primary_has_no_omop_only_patients": primary["omop_only_patients"] == 0,
        "shared_primary_index_dates_all_exact": primary["exact_date_patients"] == primary["intersection_patients"],
        "primary_source_only_categories_close": discordance_total == primary["source_only_patients"],
        "primary_source_only_fully_required_date_excluded": required_date_excluded == primary["source_only_patients"],
        "portable_partition_closes": (
            portable["intersection_patients"] + portable["source_only_patients"] == portable["source_patients"]
            and portable["intersection_patients"] + portable["native_only_patients"] == portable["native_omop_patients"]
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"Stage C D0 manuscript invariants failed: {failed}")

    cohort_rows = [
        {
            "estimand": "Primary transformation fidelity",
            "source_patients": primary["source_patients"],
            "omop_patients": primary["omop_patients"],
            "intersection_patients": primary["intersection_patients"],
            "source_only_patients": primary["source_only_patients"],
            "omop_only_patients": primary["omop_only_patients"],
            "patient_jaccard": primary["patient_jaccard"],
            "positive_agreement_percent": primary["positive_agreement_percent"],
            "exact_index_date_percent_among_shared": primary["exact_index_date_percent_among_shared"],
        },
        {
            "estimand": "Secondary native-OMOP portability sensitivity",
            "source_patients": portable["source_patients"],
            "omop_patients": portable["native_omop_patients"],
            "intersection_patients": portable["intersection_patients"],
            "source_only_patients": portable["source_only_patients"],
            "omop_only_patients": portable["native_only_patients"],
            "patient_jaccard": portable["patient_jaccard"],
            "positive_agreement_percent": portable["positive_agreement_percent"],
            "exact_index_date_percent_among_shared": None,
        },
    ]

    discordance_rows = [
        {
            "category": r["category"],
            "patients": r["patients"],
            "percent_of_primary_source_only": (
                100.0 * float(r["patients"]) / float(primary["source_only_patients"])
                if primary["source_only_patients"]
                else None
            ),
        }
        for r in categories
    ]

    cohort_csv = root / "stage_c_stroke_d0_manuscript_cohort_concordance.csv"
    discordance_csv = root / "stage_c_stroke_d0_manuscript_discordance.csv"
    _write_csv(cohort_csv, cohort_rows)
    _write_csv(discordance_csv, discordance_rows)

    disclosure_review = {
        "aggregate_only_outputs": True,
        "patient_identifiers_written": False,
        "source_record_identifiers_written": False,
        "row_level_phi_written": False,
        "free_text_values_written": False,
        "status": "passed",
        "review_note": "This D0 manuscript bundle is assembled only from aggregate preflight/concordance JSON and writes aggregate CSV/Markdown/JSON outputs only.",
    }

    final = {
        "status": "stage_c_stroke_d0_manuscript_tables_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": STUDY_DEFINITION,
        "study_definition_sha256": result["study_definition_sha256"],
        "checks": checks,
        "all_invariants_matched": all(checks.values()),
        "source_reference": source,
        "primary_transformation_fidelity": primary,
        "secondary_native_omop_portability": portable,
        "primary_source_only_discordance_categories": categories,
        "disclosure_review": disclosure_review,
        "interpretation": {
            "primary": "The lineage-faithful OMOP D0 cohort is a strict subset of the locked PCORnet D0 cohort. All shared patients have the same index date, and all primary source-only patients are explained by required diagnosis-date missingness/exclusion under the frozen ETL policy.",
            "date_policy": "The locked source phenotype permits encounter-date fallback when DX_DATE is null, whereas the frozen ETL excludes diagnoses missing the required diagnosis date. This creates expected phenotype attrition without implying unexplained transformation error.",
            "portable": "The native-OMOP portability sensitivity intentionally omits PDX because no native OMOP core equivalent exists in the frozen build; it is a portability analysis and must not replace the primary transformation-fidelity estimand.",
        },
    }

    summary_json = root / "stage_c_stroke_d0_final_summary.json"
    summary_json.write_text(json.dumps(final, indent=2, sort_keys=True), encoding="utf-8")

    md = root / "stage_c_stroke_d0_manuscript_tables.md"
    lines = [
        "# Stage C ischemic-stroke D0 manuscript tables",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        f"Locked study definition: `{STUDY_DEFINITION}`",
        "",
        "## Cohort reproducibility",
        "",
        "| Estimand | PCORnet | OMOP | Shared | PCORnet only | OMOP only | Jaccard | Positive agreement | Exact shared index date |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in cohort_rows:
        exact = r["exact_index_date_percent_among_shared"]
        exact_text = "NA" if exact is None else f"{exact:.3f}%"
        lines.append(
            f"| {r['estimand']} | {r['source_patients']:,} | {r['omop_patients']:,} | {r['intersection_patients']:,} | {r['source_only_patients']:,} | {r['omop_only_patients']:,} | {r['patient_jaccard']:.6f} | {r['positive_agreement_percent']:.3f}% | {exact_text} |"
        )

    lines += [
        "",
        "## Primary source-only discordance decomposition",
        "",
        "| Category | Patients | Percent of primary source-only |",
        "| --- | ---: | ---: |",
    ]
    for r in discordance_rows:
        pct = r["percent_of_primary_source_only"]
        pct_text = "NA" if pct is None else f"{pct:.3f}%"
        lines.append(f"| {r['category']} | {r['patients']:,} | {pct_text} |")

    lines += [
        "",
        "## Interpretation",
        "",
        f"The locked source-reference D0 cohort contained {primary['source_patients']:,} patients and the lineage-faithful frozen OMOP cohort contained {primary['omop_patients']:,}. All {primary['intersection_patients']:,} shared patients had exactly matching index dates, while {primary['source_only_patients']:,} source-only patients and {primary['omop_only_patients']:,} OMOP-only patients were observed.",
        "",
        f"All {primary['source_only_patients']:,} primary source-only patients were classified as `required_source_date_missing_or_etl_excluded`. The locked source phenotype permits index-date fallback to encounter dates when `DX_DATE` is missing, whereas the frozen ETL excludes diagnoses missing the required diagnosis date. This is therefore reported as a prespecified representation/exclusion consequence rather than unexplained transformation failure.",
        "",
        f"The native-OMOP portability sensitivity contained {portable['native_omop_patients']:,} OMOP patients, with {portable['intersection_patients']:,} shared, {portable['source_only_patients']:,} source-only, and {portable['native_only_patients']:,} native-OMOP-only patients. Because PDX is not natively represented in OMOP core, this sensitivity intentionally omits the primary-diagnosis requirement and is not the primary transformation-fidelity estimand.",
        "",
        "## Disclosure review",
        "",
        "Outputs are aggregate only. No patient identifiers, source-record identifiers, row-level PHI, or free-text clinical values are written by this bundle.",
        "",
        "All counts are observed analysis outputs, not hard-coded acceptance thresholds.",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("status: stage_c_stroke_d0_manuscript_tables_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"all_invariants_matched: {all(checks.values())}")
    print(f"disclosure_review_status: {disclosure_review['status']}")
    print(f"source_d0_patients: {primary['source_patients']}")
    print(f"lineage_faithful_omop_patients: {primary['omop_patients']}")
    print(f"source_only_patients: {primary['source_only_patients']}")
    print(f"source_only_required_date_excluded: {required_date_excluded}")
    print(f"omop_only_patients: {primary['omop_only_patients']}")
    print(f"exact_index_date_patients: {primary['exact_date_patients']}")
    print(f"summary_json: {summary_json}")
    print(f"manuscript_md: {md}")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Stage C stroke D0 manuscript tables and invariant bundle")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
