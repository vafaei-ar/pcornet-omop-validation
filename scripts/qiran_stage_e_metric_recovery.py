#!/usr/bin/env python3
"""Read-only recovery of prespecified Stage E AUPRC and association OR/95% CI.

Reads the completed institutional *aggregate* JSON. Does not run SQL, fit
models, or export row-level data. Printed aggregates require institutional
disclosure review before sharing. Run from the repository root.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
POPULATIONS = {"fixed": 3822, "source_end_to_end": 6508, "omop_end_to_end": 3822}
MODELS = ("logistic_regression", "ridge_logistic_regression", "hist_gradient_boosting")
FEATURES = ("intercept", "age_at_index_years", "female_indicator",
            "index_length_of_stay_days", "prior_365d_acute_care_encounter_count",
            "prior_365d_all_encounter_count", "prior_365d_ischemic_stroke_indicator")


def finite(value, label, lower=None, upper=None):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Missing or nonfinite " + label)
    if (lower is not None and value < lower) or (upper is not None and value > upper):
        raise ValueError("Out-of-range " + label)
    return round(float(value), 6)


def recover(payload):
    if payload.get("status") != "stage_e_statistical_model_reproducibility_complete":
        raise ValueError("Incomplete Stage E aggregate report")
    if payload.get("frozen_etl_sha") != FROZEN_SHA:
        raise ValueError("Frozen ETL SHA mismatch")
    if any(payload.get("population_sizes", {}).get(k) != v for k, v in POPULATIONS.items()):
        raise ValueError("Publication population anchors not reproduced")
    out = {
        "status": "recovered_aggregate_pending_disclosure_review",
        "analysis_git_sha": payload.get("analysis_git_sha"),
        "analysis_worktree_clean": payload.get("analysis_worktree_clean"),
        "population_sizes": payload.get("population_sizes"),
        "AUPRC": {},
        "association_or_95ci": {},
    }
    for cohort in ("fixed_cohort", "end_to_end"):
        out["AUPRC"][cohort] = {}
        out["association_or_95ci"][cohort] = {}
        models = payload["prediction_reproducibility"][cohort]
        for name in MODELS:
            estimates = {}
            for representation in ("source", "omop"):
                estimates[representation] = finite(
                    models[name][representation]["AUPRC"],
                    f"{cohort}/{name}/{representation}/AUPRC", 0.0, 1.0)
            estimates["omop_minus_source"] = round(
                estimates["omop"] - estimates["source"], 6)
            out["AUPRC"][cohort][name] = estimates
        terms = payload["association_reproducibility"][cohort]
        for name in FEATURES:
            estimates = {}
            for representation in ("source", "omop"):
                rec = terms[name][representation]
                lower = finite(rec["ci95_lower"], f"{name}/{representation}/lower", 0)
                odds = finite(rec["odds_ratio"], f"{name}/{representation}/OR", 0)
                upper = finite(rec["ci95_upper"], f"{name}/{representation}/upper", 0)
                if not lower <= odds <= upper:
                    raise ValueError(f"OR outside its 95% CI for {cohort}/{name}/{representation}")
                estimates[representation] = {"OR": odds, "lower95": lower, "upper95": upper}
            estimates["or_ratio_omop_over_source"] = finite(
                terms[name]["odds_ratio_ratio_omop_over_source"],
                f"{cohort}/{name}/OR ratio", 0)
            out["association_or_95ci"][cohort][name] = estimates
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="Path to original completed stage_e_statistical_model_reproducibility.json")
    args = parser.parse_args()
    print(json.dumps(recover(json.loads(Path(args.input).read_text(encoding="utf-8"))),
                     indent=2, sort_keys=True))
    print("Disclosure review required before sharing any printed aggregate results.", file=sys.stderr)


if __name__ == "__main__":
    main()
