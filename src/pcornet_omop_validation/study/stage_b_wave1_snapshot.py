from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _load(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"Missing required Stage B output: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run(config_path: str) -> dict[str, object]:
    config = load_etl_config(config_path)
    root = config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance"
    encounter = _load(root / "encounter" / "stage_b_encounter_summary.json")
    death = _load(root / "death" / "stage_b_death_summary.json")
    condition = _load(root / "condition" / "stage_b_condition_summary.json")
    condition_attr = _load(root / "condition" / "stage_b_condition_attribution.json")
    procedure = _load(root / "procedure" / "stage_b_procedure_summary.json")

    for name, payload in {
        "encounter": encounter,
        "death": death,
        "condition": condition,
        "condition_attribution": condition_attr,
        "procedure": procedure,
    }.items():
        if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
            raise RuntimeError(f"{name} output is not anchored to frozen ETL SHA")

    ep = encounter["primary_comparison"]
    dp = death["primary_comparison"]
    cp = condition["primary_comparison"]
    ca = condition_attr["totals"]
    pp = procedure["primary_comparison"]

    snapshot = {
        "status": "stage_b_wave1_snapshot_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "encounter": {
            "source_events": ep["source_events"],
            "target_events": ep["target_events"],
            "patient_jaccard": ep["patient_jaccard"],
            "source_unmatched_date_events": ep["source_unmatched_date_events"],
            "target_unmatched_date_events": ep["target_unmatched_date_events"],
        },
        "death": {
            "source_events": dp["source_events"],
            "target_events": dp["target_events"],
            "patient_jaccard": dp["patient_jaccard"],
            "discordant_date_pairs": dp["discordant_date_pairs"],
        },
        "condition": {
            "source_mapped_route_rows": cp["source_mapped_route_rows"],
            "exact_matched_events": cp["exact_person_date_domain_concept_matched_events"],
            "source_unmatched_events": cp["source_unmatched_signature_events"],
            "target_unmatched_rows_in_same_concept_space": cp["target_unmatched_signature_events"],
            "condition_derived_rows": ca["condition_derived_rows"],
            "other_provenance_rows": ca["other_provenance_rows"],
            "patient_jaccard_before_attribution": cp["patient_jaccard"],
        },
        "procedure": {
            "source_mapped_event_route_rows": pp["source_mapped_event_route_rows"],
            "exact_matched_events": pp["exact_person_date_domain_concept_matched_events"],
            "source_unmatched_events": pp["source_unmatched_signature_events"],
            "target_unmatched_rows_in_same_concept_space": pp["target_unmatched_signature_events"],
            "patient_jaccard_before_attribution": pp["patient_jaccard"],
            "unresolved_rows": pp["source_unresolved_route_rows"],
            "non_event_semantic_component_rows": pp["source_non_event_semantic_component_rows"],
        },
    }

    out = root / "stage_b_wave1_snapshot.json"
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    print("status: stage_b_wave1_snapshot_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"encounter_patient_jaccard: {snapshot['encounter']['patient_jaccard']}")
    print(f"death_patient_jaccard: {snapshot['death']['patient_jaccard']}")
    print(f"condition_source_unmatched_events: {snapshot['condition']['source_unmatched_events']}")
    print(f"condition_other_provenance_rows: {snapshot['condition']['other_provenance_rows']}")
    print(f"procedure_source_unmatched_events: {snapshot['procedure']['source_unmatched_events']}")
    print(f"procedure_target_unmatched_rows_in_same_concept_space: {snapshot['procedure']['target_unmatched_rows_in_same_concept_space']}")
    print(f"output: {out}")
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a combined Stage B Wave 1 snapshot from completed domain outputs")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
