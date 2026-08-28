from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _load(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"Missing required Wave 2 output: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError(f"Output is not anchored to frozen ETL SHA: {path}")
    return payload


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    root = config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance"
    drug_dir = root / "drug"
    mo_dir = root / "measurement_observation"

    drug = _load(drug_dir / "stage_b_wave2_drug_summary.json")
    drug_attr = _load(drug_dir / "stage_b_wave2_drug_attribution.json")
    mo = _load(mo_dir / "stage_b_wave2_measurement_observation_summary.json")
    values = _load(mo_dir / "stage_b_wave2_measurement_observation_value_layers.json")
    vital_diag = _load(mo_dir / "stage_b_wave2_vital_numeric_diagnostic.json")

    dp = drug["primary_comparison"]
    da = drug_attr["totals"]
    mp = mo["primary_comparison"]
    nv = values["numeric_value"]
    uv = values["unit"]
    cv = values["categorical_value"]
    vd = vital_diag["overall"]

    checks = {
        "drug_all_mapped_source_routes_matched": (
            dp["source_unmatched_signature_events"] == 0
            and dp["exact_person_date_concept_matched_events"] == dp["source_mapped_route_rows"]
        ),
        "drug_attribution_closes_target_excess": (
            da["base_drug_derived_rows"] == dp["source_mapped_route_rows"]
            and da["other_provenance_rows"] == dp["target_unmatched_signature_events"]
        ),
        "measurement_observation_semantic_space_balanced": (
            mp["source_mapped_rows"] == mp["target_native_rows_in_source_concept_space"]
        ),
        "measurement_observation_all_mapped_events_matched": (
            mp["source_unmatched_signature_events"] == 0
            and mp["target_unmatched_signature_events"] == 0
            and mp["exact_person_date_domain_concept_matched_events"] == mp["source_mapped_rows"]
        ),
        "measurement_observation_patient_sets_match": (
            mp["source_only_patients"] == 0
            and mp["target_only_patients"] == 0
            and mp["patient_jaccard"] == 1.0
        ),
        "resolved_ucum_units_all_agree": (
            uv["resolved_disagreement_rows"] == 0
            and uv["exact_agreement_rows"] == uv["resolved_standard_ucum_rows"]
        ),
        "mapped_categorical_values_all_agree": (
            cv["mapped_disagreement_rows"] == 0
            and cv["exact_mapped_agreement_rows"] == cv["mapped_value_rows"]
            and cv["unexpected_nonzero_target_rows_for_zero_policy"] == 0
        ),
        "vital_target_reproduces_frozen_etl_numeric_expression": vd["expanded_target_mismatch_rows"] == 0,
        "vital_direct_source_differences_fully_explained_by_etl_expression": vd["mismatches_not_explained_by_etl_expansion_rows"] == 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"Wave 2 manuscript invariants failed: {failed}")

    primary_rows = [
        {
            "semantic_family": "Drug",
            "source_mapped_rows": dp["source_mapped_route_rows"],
            "exact_matched_rows": dp["exact_person_date_concept_matched_events"],
            "source_unmatched_rows": dp["source_unmatched_signature_events"],
            "target_rows_in_source_concept_space": dp["target_native_rows_in_source_concept_space"],
            "target_unmatched_before_attribution": dp["target_unmatched_signature_events"],
            "other_provenance_rows": da["other_provenance_rows"],
            "unresolved_or_concept_zero_rows": dp["source_unresolved_route_rows"],
            "patient_jaccard": dp["patient_jaccard"],
            "source_exact_match_percent": dp["source_exact_signature_match_percent"],
        },
        {
            "semantic_family": "Measurement/Observation",
            "source_mapped_rows": mp["source_mapped_rows"],
            "exact_matched_rows": mp["exact_person_date_domain_concept_matched_events"],
            "source_unmatched_rows": mp["source_unmatched_signature_events"],
            "target_rows_in_source_concept_space": mp["target_native_rows_in_source_concept_space"],
            "target_unmatched_before_attribution": mp["target_unmatched_signature_events"],
            "other_provenance_rows": 0,
            "unresolved_or_concept_zero_rows": (
                mo["unresolved_or_descriptive_coverage"]["OBS_CLIN"]
                + mo["unresolved_or_descriptive_coverage"]["PROCEDURES"]
                + mo["unresolved_or_descriptive_coverage"]["OBS_GEN_descriptive_concept_zero"]
            ),
            "patient_jaccard": mp["patient_jaccard"],
            "source_exact_match_percent": mp["source_exact_signature_match_percent"],
        },
    ]

    value_rows = [
        {
            "layer": "Numeric value, direct-source expression",
            "denominator_rows": nv["directly_comparable_rows"],
            "exact_agreement_rows": nv["exact_match_rows"],
            "discordant_rows": nv["mismatch_rows"],
            "coverage_or_exact_percent": nv["exact_match_percent"],
            "interpretation": "LAB and OBS_CLIN exact; VITAL direct-source differences are fully explained by the frozen ETL SQL expression and are not unexplained target divergence.",
        },
        {
            "layer": "UCUM resolved unit",
            "denominator_rows": uv["source_rows_with_unit_semantics"],
            "exact_agreement_rows": uv["exact_agreement_rows"],
            "discordant_rows": uv["resolved_disagreement_rows"],
            "coverage_or_exact_percent": uv["standard_ucum_coverage_percent"],
            "interpretation": "Coverage percent is active Standard UCUM resolution coverage; exact agreement among resolved units is 100%.",
        },
        {
            "layer": "Categorical mapped value concept",
            "denominator_rows": cv["categorical_rows"],
            "exact_agreement_rows": cv["exact_mapped_agreement_rows"],
            "discordant_rows": cv["mapped_disagreement_rows"],
            "coverage_or_exact_percent": cv["mapped_value_coverage_percent"],
            "interpretation": "Coverage percent is prespecified exact Standard value-concept coverage; agreement among mapped values is 100%, with zero-policy rows remaining concept 0.",
        },
    ]

    vital_rows: list[dict[str, object]] = []
    for r in vital_diag["by_field"]:
        vital_rows.append({
            "source_field": r["source_field"],
            "rows": r["rows"],
            "direct_target_mismatch_rows": r["direct_target_mismatch_rows"],
            "expanded_target_mismatch_rows": r["expanded_target_mismatch_rows"],
            "mismatches_explained_by_etl_expansion_rows": r["mismatches_explained_by_etl_expansion_rows"],
            "mismatches_not_explained_by_etl_expansion_rows": r["mismatches_not_explained_by_etl_expansion_rows"],
            "max_direct_target_abs_difference": r["max_direct_target_abs_difference"],
        })

    primary_csv = root / "stage_b_wave2_manuscript_primary.csv"
    values_csv = root / "stage_b_wave2_manuscript_value_layers.csv"
    vital_csv = root / "stage_b_wave2_vital_numeric_representation.csv"
    _write_csv(primary_csv, primary_rows)
    _write_csv(values_csv, value_rows)
    _write_csv(vital_csv, vital_rows)

    disclosure_review = {
        "aggregate_only_outputs": True,
        "row_level_phi_written": False,
        "patient_identifiers_written": False,
        "source_record_identifiers_written": False,
        "free_text_values_written": False,
        "review_note": "This manuscript bundle is assembled only from aggregate JSON/CSV analysis outputs. It does not export row-level patient, source-record, or free-text data.",
        "status": "passed",
    }

    final = {
        "status": "stage_b_wave2_manuscript_tables_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "checks": checks,
        "all_invariants_matched": all(checks.values()),
        "primary_rows": primary_rows,
        "value_layer_rows": value_rows,
        "vital_numeric_representation": {
            "direct_target_mismatch_rows": vd["direct_target_mismatch_rows"],
            "expanded_target_mismatch_rows": vd["expanded_target_mismatch_rows"],
            "mismatches_explained_by_etl_expansion_rows": vd["mismatches_explained_by_etl_expansion_rows"],
            "mismatches_not_explained_by_etl_expansion_rows": vd["mismatches_not_explained_by_etl_expansion_rows"],
            "max_direct_target_abs_difference": vd["max_direct_target_abs_difference"],
            "interpretation": "The stored OMOP VITAL values exactly reproduce the frozen ETL CROSS APPLY VALUES numeric expression. The direct native-field differences are deterministic expression/coercion effects, not unexplained target divergence.",
        },
        "coverage": {
            "drug_unresolved_concept_zero_rows": dp["source_unresolved_route_rows"],
            "measurement_observation_unresolved_or_descriptive": mo["unresolved_or_descriptive_coverage"],
            "ucum_resolved_rows": uv["resolved_standard_ucum_rows"],
            "ucum_unresolved_rows": uv["unresolved_ucum_rows"],
            "categorical_mapped_value_rows": cv["mapped_value_rows"],
            "categorical_concept_zero_policy_rows": cv["concept_zero_policy_rows"],
        },
        "disclosure_review": disclosure_review,
        "interpretation": {
            "drug": "All mapped nonzero Drug semantic routes are present exactly; the small target-native excess is fully explained by other audited provenance.",
            "measurement_observation": "All mapped Measurement/Observation semantic events are present exactly in native OMOP under the prespecified person/date/domain/concept identity, with no target excess and no unattributed mapped rows.",
            "numeric": "Directly comparable LAB and OBS_CLIN numeric values are exact. VITAL target values exactly reproduce the frozen ETL SQL expression; direct native-field differences are representation effects from that expression.",
            "unit": "All uniquely resolved active Standard UCUM units agree exactly; unresolved units are reported as vocabulary coverage rather than event-level semantic failure.",
            "categorical": "All prespecified mapped categorical Standard value concepts agree exactly; unsupported values remain concept zero under the frozen policy.",
        },
    }

    out_json = root / "stage_b_wave2_final_summary.json"
    out_json.write_text(json.dumps(final, indent=2, sort_keys=True), encoding="utf-8")

    md = root / "stage_b_wave2_manuscript_tables.md"
    lines = [
        "# Stage B Wave 2 manuscript tables",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "## Primary semantic concordance",
        "",
        "| Semantic family | Source mapped rows | Exact matched | Source unmatched | Target rows in source concept space | Target excess before attribution | Other provenance | Unresolved / concept zero | Patient Jaccard |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in primary_rows:
        lines.append(
            f"| {r['semantic_family']} | {r['source_mapped_rows']:,} | {r['exact_matched_rows']:,} | {r['source_unmatched_rows']:,} | {r['target_rows_in_source_concept_space']:,} | {r['target_unmatched_before_attribution']:,} | {r['other_provenance_rows']:,} | {r['unresolved_or_concept_zero_rows']:,} | {r['patient_jaccard']:.6f} |"
        )
    lines += [
        "",
        "## Secondary value and unit layers",
        "",
        "| Layer | Denominator | Exact agreement | Discordant | Coverage / exact percent |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in value_rows:
        pct = r["coverage_or_exact_percent"]
        pct_text = "" if pct is None else f"{pct:.6f}"
        lines.append(
            f"| {r['layer']} | {r['denominator_rows']:,} | {r['exact_agreement_rows']:,} | {r['discordant_rows']:,} | {pct_text} |"
        )
    lines += [
        "",
        "## VITAL numeric representation diagnostic",
        "",
        f"The direct native-field comparison identified {vd['direct_target_mismatch_rows']:,} differences. Reproducing the frozen ETL `CROSS APPLY (VALUES ...)` expression reduced target mismatches to {vd['expanded_target_mismatch_rows']:,}; all {vd['mismatches_explained_by_etl_expansion_rows']:,} direct differences were explained by that expression, leaving {vd['mismatches_not_explained_by_etl_expansion_rows']:,} unexplained rows.",
        "",
        "This is reported as a deterministic value-representation effect of the frozen SQL expression, not as unexplained OMOP divergence. No post-hoc tolerance was introduced.",
        "",
        "## Disclosure review",
        "",
        "Manuscript outputs are aggregate-only. No patient identifiers, source record identifiers, row-level PHI, or free-text clinical values are written by this bundle.",
        "",
        "All counts are analysis outputs and coverage results, not hard-coded acceptance thresholds.",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("status: stage_b_wave2_manuscript_tables_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"all_invariants_matched: {all(checks.values())}")
    print(f"disclosure_review_status: {disclosure_review['status']}")
    print(f"drug_source_unmatched_rows: {dp['source_unmatched_signature_events']}")
    print(f"measurement_observation_source_unmatched_rows: {mp['source_unmatched_signature_events']}")
    print(f"measurement_observation_target_unmatched_rows: {mp['target_unmatched_signature_events']}")
    print(f"vital_direct_target_mismatch_rows: {vd['direct_target_mismatch_rows']}")
    print(f"vital_expanded_target_mismatch_rows: {vd['expanded_target_mismatch_rows']}")
    print(f"vital_unexplained_numeric_mismatch_rows: {vd['mismatches_not_explained_by_etl_expansion_rows']}")
    print(f"summary_json: {out_json}")
    print(f"manuscript_md: {md}")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final Stage B Wave 2 manuscript-oriented tables and invariant bundle")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
