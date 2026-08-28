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
        raise RuntimeError(f"Missing required Stage B output: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError(f"Output is not anchored to frozen ETL SHA: {path}")
    return payload


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def run(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    root = config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance"

    encounter = _load(root / "encounter" / "stage_b_encounter_summary.json")
    death = _load(root / "death" / "stage_b_death_summary.json")
    condition = _load(root / "condition" / "stage_b_condition_summary.json")
    condition_attr = _load(root / "condition" / "stage_b_condition_attribution.json")
    procedure = _load(root / "procedure" / "stage_b_procedure_summary.json")
    procedure_attr = _load(root / "procedure" / "stage_b_procedure_attribution.json")

    ep = encounter["primary_comparison"]
    dp = death["primary_comparison"]
    cp = condition["primary_comparison"]
    ca = condition_attr["totals"]
    pp = procedure["primary_comparison"]
    pa = procedure_attr["totals"]

    checks = {
        "encounter_source_target_rows": ep["source_events"] == ep["target_events"],
        "encounter_no_unmatched_dates": ep["source_unmatched_date_events"] == 0 and ep["target_unmatched_date_events"] == 0,
        "death_source_target_rows": dp["source_events"] == dp["target_events"],
        "death_no_date_discordance": dp["discordant_date_pairs"] == 0,
        "condition_all_mapped_source_events_matched": cp["source_unmatched_signature_events"] == 0 and cp["exact_person_date_domain_concept_matched_events"] == cp["source_mapped_route_rows"],
        "condition_attribution_closes_target_excess": ca["condition_derived_rows"] == cp["source_mapped_route_rows"] and ca["other_provenance_rows"] == cp["target_unmatched_signature_events"],
        "procedure_all_mapped_source_events_matched": pp["source_unmatched_signature_events"] == 0 and pp["exact_person_date_domain_concept_matched_events"] == pp["source_mapped_event_route_rows"],
        "procedure_attribution_closes_target_excess": pa["procedure_derived_rows"] == pp["source_mapped_event_route_rows"] and pa["other_provenance_rows"] == pp["target_unmatched_signature_events"],
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"Wave 1 manuscript invariants failed: {failed}")

    rows = [
        {
            "domain": "Encounter",
            "source_events_or_mapped_routes": ep["source_events"],
            "exact_matched_events": ep["exact_date_matched_events"],
            "source_unmatched_events": ep["source_unmatched_date_events"],
            "target_rows_in_same_semantic_space": ep["target_events"],
            "other_provenance_rows": 0,
            "unresolved_rows": 0,
            "non_event_semantic_rows": 0,
            "patient_jaccard_before_attribution": ep["patient_jaccard"],
            "source_event_match_percent": _pct(ep["exact_date_matched_events"], ep["source_events"]),
        },
        {
            "domain": "Death",
            "source_events_or_mapped_routes": dp["source_events"],
            "exact_matched_events": dp["exact_date_matches"],
            "source_unmatched_events": dp["discordant_date_pairs"],
            "target_rows_in_same_semantic_space": dp["target_events"],
            "other_provenance_rows": 0,
            "unresolved_rows": 0,
            "non_event_semantic_rows": 0,
            "patient_jaccard_before_attribution": dp["patient_jaccard"],
            "source_event_match_percent": _pct(dp["exact_date_matches"], dp["source_events"]),
        },
        {
            "domain": "Condition semantics",
            "source_events_or_mapped_routes": cp["source_mapped_route_rows"],
            "exact_matched_events": cp["exact_person_date_domain_concept_matched_events"],
            "source_unmatched_events": cp["source_unmatched_signature_events"],
            "target_rows_in_same_semantic_space": cp["target_native_rows_in_source_concept_space"],
            "other_provenance_rows": ca["other_provenance_rows"],
            "unresolved_rows": cp["source_unresolved_fallback_rows"],
            "non_event_semantic_rows": 0,
            "patient_jaccard_before_attribution": cp["patient_jaccard"],
            "source_event_match_percent": _pct(cp["exact_person_date_domain_concept_matched_events"], cp["source_mapped_route_rows"]),
        },
        {
            "domain": "Procedure semantics",
            "source_events_or_mapped_routes": pp["source_mapped_event_route_rows"],
            "exact_matched_events": pp["exact_person_date_domain_concept_matched_events"],
            "source_unmatched_events": pp["source_unmatched_signature_events"],
            "target_rows_in_same_semantic_space": pp["target_native_rows_in_source_concept_space"],
            "other_provenance_rows": pa["other_provenance_rows"],
            "unresolved_rows": pp["source_unresolved_route_rows"],
            "non_event_semantic_rows": pp["source_non_event_semantic_component_rows"],
            "patient_jaccard_before_attribution": pp["patient_jaccard"],
            "source_event_match_percent": _pct(pp["exact_person_date_domain_concept_matched_events"], pp["source_mapped_event_route_rows"]),
        },
    ]

    out_csv = root / "stage_b_wave1_manuscript_summary.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    # Preserve per-domain semantic-space results and attribution side by side.
    domain_rows: list[dict[str, object]] = []
    for label, primary, attr in (
        ("Condition", condition["domain_summary"], condition_attr["domain_summary"]),
        ("Procedure", procedure["domain_summary"], procedure_attr["domain_summary"]),
    ):
        attr_by_domain = {r["target_domain"]: r for r in attr}
        for r in primary:
            a = attr_by_domain[r["target_domain"]]
            domain_rows.append({
                "semantic_family": label,
                "target_domain": r["target_domain"],
                "source_mapped_rows": r["source_mapped_route_rows"],
                "exact_matched_rows": r["exact_signature_matched_events"],
                "source_unmatched_rows": r["source_unmatched_events"],
                "target_rows_in_source_concept_space": r["target_native_rows_in_source_concept_space"],
                "target_unmatched_before_attribution": r["target_unmatched_events"],
                "other_provenance_rows": a["other_provenance_rows"],
            })
    out_domain = root / "stage_b_wave1_domain_attribution.csv"
    with out_domain.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(domain_rows[0].keys()))
        w.writeheader(); w.writerows(domain_rows)

    final = {
        "status": "stage_b_wave1_manuscript_tables_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "checks": checks,
        "summary_rows": rows,
        "interpretation": {
            "encounter": "Exact native-CDM patient/event/date concordance.",
            "death": "Exact native-CDM patient/date concordance.",
            "condition": "All mapped source semantic routes are present exactly; target-native excess in the same concept space is explained by other audited provenance.",
            "procedure": "All mapped source semantic routes are present exactly; target-native excess in the same concept space is explained by other audited provenance.",
            "general": "Native OMOP concept spaces can legitimately contain events from multiple PCORnet source families; concept-space target excess is therefore not synonymous with transformation error.",
        },
    }
    out_json = root / "stage_b_wave1_final_summary.json"
    out_json.write_text(json.dumps(final, indent=2, sort_keys=True), encoding="utf-8")

    md = root / "stage_b_wave1_manuscript_tables.md"
    lines = [
        "# Stage B Wave 1 manuscript tables",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "## Primary patient/event concordance",
        "",
        "| Semantic family | Source events / mapped routes | Exact matched | Source unmatched | Target rows in same semantic space | Other provenance | Unresolved | Patient Jaccard before attribution |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r['domain']} | {r['source_events_or_mapped_routes']:,} | {r['exact_matched_events']:,} | {r['source_unmatched_events']:,} | {r['target_rows_in_same_semantic_space']:,} | {r['other_provenance_rows']:,} | {r['unresolved_rows']:,} | {r['patient_jaccard_before_attribution']:.6f} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "For Condition and Procedure, the primary native-target concept-space comparison is intentionally broader than lineage-restricted comparison. All mapped source semantic events must first be found independently in native OMOP. Secondary lineage attribution is then used only to explain additional native OMOP rows in the same concept space. This separation prevents lineage from defining the primary result while distinguishing legitimate multi-source OMOP representation from ETL loss or fabrication.",
        "",
        "All reported counts are analysis outputs, not acceptance thresholds.",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("status: stage_b_wave1_manuscript_tables_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"all_invariants_matched: {all(checks.values())}")
    print(f"summary_csv: {out_csv}")
    print(f"domain_csv: {out_domain}")
    print(f"summary_json: {out_json}")
    print(f"manuscript_md: {md}")
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final Stage B Wave 1 manuscript-oriented tables")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
