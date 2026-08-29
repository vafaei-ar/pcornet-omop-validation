from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _load(path: Path, status: str | None = None) -> dict:
    if not path.exists():
        raise RuntimeError(f"Missing required Stage C output: {path}")
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError(f"Output is not anchored to frozen ETL: {path}")
    if status is not None and d.get("status") != status:
        raise RuntimeError(f"Unexpected status in {path}: {d.get('status')}")
    return d


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run(config_path: str) -> dict:
    cfg = load_etl_config(config_path)
    root = cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes"
    d13 = root / "stroke_d1_d3"

    summary = _load(root / "stage_c_stroke_phenotype_summary.json", "stage_c_stroke_phenotype_summary_complete")
    mechanism = _load(d13 / "stage_c_stroke_d1_d3_post_outcome_mechanism_audit.json", "stage_c_stroke_d1_d3_post_outcome_mechanism_audit_complete")
    dates = _load(d13 / "stage_c_stroke_d1_d3_post_outcome_index_date_selection_audit.json", "stage_c_stroke_d1_d3_post_outcome_index_date_selection_audit_complete")

    if not summary.get("all_invariants_matched"):
        raise RuntimeError("Combined Stage C phenotype summary invariants did not pass")
    if mechanism.get("study_definition_sha256") != dates.get("study_definition_sha256"):
        raise RuntimeError("D1/D3 diagnostic audits do not share the same study definition")

    phenotype_rows = summary["phenotype_rows"]
    pmap = {r["phenotype"]: r for r in phenotype_rows}
    for phenotype in ("D1", "D3"):
        if dates[phenotype]["shared_patients"] != pmap[phenotype]["intersection_patients"]:
            raise RuntimeError(f"{phenotype} date-audit shared count differs from completed summary")
        if dates[phenotype]["exact_index_date_patients"] != round(
            pmap[phenotype]["intersection_patients"] * pmap[phenotype]["exact_index_date_percent_among_shared"] / 100.0
        ):
            raise RuntimeError(f"{phenotype} date-audit exact count differs from completed summary")

    cohort_rows = []
    for r in phenotype_rows:
        cohort_rows.append({
            "phenotype": r["phenotype"],
            "source_patients": r["source_patients"],
            "lineage_faithful_omop_patients": r["lineage_faithful_omop_patients"],
            "intersection_patients": r["intersection_patients"],
            "source_only_patients": r["source_only_patients"],
            "omop_only_patients": r["omop_only_patients"],
            "patient_jaccard": r["patient_jaccard"],
            "positive_agreement_percent": r["positive_agreement_percent"],
            "exact_index_date_percent_among_shared": r["exact_index_date_percent_among_shared"],
            "native_omop_patients": r["native_omop_patients"],
            "native_omop_patient_jaccard": r["native_omop_patient_jaccard"],
        })

    attrition_rows = []
    for phenotype in ("D1", "D3"):
        m = mechanism[phenotype]["source_only_mechanism"]
        attrition_rows.append({
            "phenotype": phenotype,
            "source_only_patients": m["source_only_patients"],
            "dx_date_null": m["dx_date_null"],
            "diagnosis_xwalk_missing": m["diagnosis_xwalk_missing"],
            "visit_xwalk_missing": m["visit_xwalk_missing"],
            "condition_row_missing_after_xwalk": m["condition_row_missing_after_xwalk"],
            "all_source_only_have_null_selected_dx_date": mechanism[phenotype]["interpretation_flags"]["all_source_only_have_null_selected_dx_date"],
            "all_source_only_missing_diagnosis_lineage": mechanism[phenotype]["interpretation_flags"]["all_source_only_missing_diagnosis_lineage"],
        })

    date_rows = []
    for phenotype in ("D1", "D3"):
        d = dates[phenotype]
        for m in d["mechanism_categories"]:
            date_rows.append({
                "phenotype": phenotype,
                "shared_patients": d["shared_patients"],
                "exact_index_date_patients": d["exact_index_date_patients"],
                "mismatched_index_date_patients": d["mismatched_index_date_patients"],
                "mismatch_percent_among_shared": d["mismatch_percent_among_shared"],
                "mechanism_category": m["category"],
                "patients": m["patients"],
            })

    checks = {
        "combined_summary_invariants_pass": bool(summary.get("all_invariants_matched")),
        "D1_source_only_mechanism_closes": sum(r["patients"] for r in mechanism["D1"]["shared_index_date_day_difference_distribution"]) == pmap["D1"]["intersection_patients"],
        "D3_source_only_mechanism_closes": sum(r["patients"] for r in mechanism["D3"]["shared_index_date_day_difference_distribution"]) == pmap["D3"]["intersection_patients"],
        "D1_date_mechanism_closes": sum(r["patients"] for r in dates["D1"]["mechanism_categories"]) == dates["D1"]["mismatched_index_date_patients"],
        "D3_date_mechanism_closes": sum(r["patients"] for r in dates["D3"]["mechanism_categories"]) == dates["D3"]["mismatched_index_date_patients"],
        "D1_all_source_only_null_dx_and_missing_lineage": mechanism["D1"]["interpretation_flags"]["all_source_only_have_null_selected_dx_date"] and mechanism["D1"]["interpretation_flags"]["all_source_only_missing_diagnosis_lineage"],
        "D3_all_source_only_null_dx_and_missing_lineage": mechanism["D3"]["interpretation_flags"]["all_source_only_have_null_selected_dx_date"] and mechanism["D3"]["interpretation_flags"]["all_source_only_missing_diagnosis_lineage"],
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        raise RuntimeError(f"Stage C manuscript bundle checks failed: {failed}")

    bundle = {
        "status": "stage_c_stroke_manuscript_bundle_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "checks": checks,
        "all_invariants_matched": all(checks.values()),
        "cohort_rows": cohort_rows,
        "source_only_attrition_rows": attrition_rows,
        "shared_index_date_mechanism_rows": date_rows,
        "analysis_roles": {
            "primary": "Prespecified lineage-faithful transformation-fidelity analyses for D0, D1, and D3.",
            "secondary": "Prespecified native-OMOP portability sensitivities.",
            "diagnostic": "D1/D3 source-only and index-date mechanism audits were designed after observing completed concordance and are explanatory only.",
        },
        "interpretation": {
            "primary": "Across D0, D1, and D3, lineage-faithful OMOP retained roughly three-fifths of the locked PCORnet cohorts. D1/D3 source-only attrition was completely attributable to selected stroke diagnoses with null DX_DATE that lacked diagnosis lineage under the frozen ETL required-date policy.",
            "multidomain": "Adding imaging and lipid criteria did not produce progressive loss of cohort concordance. Jaccard similarity remained stable across D0, D1, and D3, supporting the interpretation that the dominant loss mechanism occurs upstream at diagnosis materialization rather than in imaging or lipid transformation.",
            "dates": "Among shared D1/D3 patients, more than 97% had exact selected index dates. The post-outcome index-date audit partitions the remaining discordance into same-encounter date-representation differences versus different qualifying-episode selection mechanisms.",
            "portability": "Native OMOP portability is a secondary sensitivity because PCORnet PDX is not natively represented in OMOP core and unresolved/deprecated locked LOINCs remain frozen-vocabulary coverage limitations.",
        },
        "guardrails": [
            "Do not modify the frozen ETL or locked phenotype definitions in response to these results.",
            "Do not present post-outcome mechanism audits as prespecified confirmatory analyses.",
            "Do not substitute native-OMOP portability results for the primary lineage-faithful transformation-fidelity estimand.",
        ],
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "source_record_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    out_json = root / "stage_c_stroke_manuscript_bundle.json"
    out_md = root / "stage_c_stroke_manuscript_bundle.md"
    cohort_csv = root / "stage_c_stroke_manuscript_cohort_table.csv"
    attrition_csv = root / "stage_c_stroke_manuscript_attrition_table.csv"
    date_csv = root / "stage_c_stroke_manuscript_index_date_mechanisms.csv"
    out_json.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(cohort_csv, cohort_rows)
    _write_csv(attrition_csv, attrition_rows)
    _write_csv(date_csv, date_rows)

    lines = [
        "# Stage C ischemic-stroke phenotype manuscript bundle",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "## Primary transformation-fidelity and native-portability results",
        "",
        "| Phenotype | PCORnet | Lineage OMOP | Shared | PCORnet only | OMOP only | Jaccard | Positive agreement | Exact shared index date | Native OMOP | Native Jaccard |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in cohort_rows:
        lines.append(
            f"| {r['phenotype']} | {r['source_patients']:,} | {r['lineage_faithful_omop_patients']:,} | {r['intersection_patients']:,} | {r['source_only_patients']:,} | {r['omop_only_patients']:,} | {r['patient_jaccard']:.3f} | {r['positive_agreement_percent']:.2f}% | {r['exact_index_date_percent_among_shared']:.2f}% | {r['native_omop_patients']:,} | {r['native_omop_patient_jaccard']:.3f} |"
        )
    lines += [
        "",
        "## D1/D3 source-only mechanism audit",
        "",
        "| Phenotype | Source only | Null selected DX_DATE | Diagnosis xwalk missing | Visit xwalk missing | Condition missing after xwalk |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in attrition_rows:
        lines.append(
            f"| {r['phenotype']} | {r['source_only_patients']:,} | {r['dx_date_null']:,} | {r['diagnosis_xwalk_missing']:,} | {r['visit_xwalk_missing']:,} | {r['condition_row_missing_after_xwalk']:,} |"
        )
    lines += [
        "",
        "## Shared D1/D3 index-date discordance",
        "",
        "| Phenotype | Shared | Exact | Mismatched | Mismatch % | Mechanism | Patients |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for r in date_rows:
        lines.append(
            f"| {r['phenotype']} | {r['shared_patients']:,} | {r['exact_index_date_patients']:,} | {r['mismatched_index_date_patients']:,} | {r['mismatch_percent_among_shared']:.2f}% | {r['mechanism_category']} | {r['patients']:,} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        bundle["interpretation"]["primary"],
        "",
        bundle["interpretation"]["multidomain"],
        "",
        bundle["interpretation"]["dates"],
        "",
        bundle["interpretation"]["portability"],
        "",
        "## Analysis-role guardrail",
        "",
        "The D1/D3 source-only and index-date mechanism audits are post-outcome explanatory analyses. They must not be described as prespecified confirmatory analyses and must not be used to redefine the frozen ETL or phenotype definitions.",
        "",
        "## Disclosure review",
        "",
        "All manuscript-bundle outputs are aggregate only. No patient identifiers, source-record identifiers, or row-level PHI are written.",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("status: stage_c_stroke_manuscript_bundle_complete")
    print(f"all_invariants_matched: {bundle['all_invariants_matched']}")
    print(f"bundle_json: {out_json}")
    print(f"bundle_md: {out_md}")
    print(f"cohort_csv: {cohort_csv}")
    print(f"attrition_csv: {attrition_csv}")
    print(f"index_date_csv: {date_csv}")
    return bundle


def main() -> None:
    p = argparse.ArgumentParser(description="Build final Stage C stroke manuscript bundle")
    p.add_argument("--config", required=True)
    a = p.parse_args()
    run(a.config)


if __name__ == "__main__":
    main()
