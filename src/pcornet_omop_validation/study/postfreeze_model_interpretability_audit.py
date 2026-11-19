"""Validate completed private RF/XGBoost aggregate without disclosing scores.

This does not fit models, query SQL, read row-level Parquet, or write output.
Only PASS / BLOCKED labels are printed. The JSON remains private and no
claim of statistical significance follows from descriptive importance ranks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys

from pcornet_omop_validation.study import postfreeze_model_interpretability as m

FILE = m.PRIVATE_RESULTS_ROOT / "aggregate_results.json"
MODELS = ("random_forest", "xgboost")
SENSITIVITIES = {
    "locked_six_features": m.FEATURES,
    "drop_date_sensitive_prior_stroke": [
        f for f in m.FEATURES if f != "prior_365d_ischemic_stroke_indicator"
    ],
}
PAIRS = {
    "end_to_end": ("pcornet_full", "omop_frozen", "native_test"),
    "selection_within_pcornet": ("pcornet_full", "pcornet_shared", "common_shared_test"),
    "representation_fixed": ("pcornet_shared", "omop_frozen", "common_shared_test"),
    "representation_fallback": ("pcornet_full", "omop_fallback", "native_test"),
}
METRICS = ("auroc", "auprc", "brier", "prevalence")


class AuditBlocked(RuntimeError):
    """No file detail, patient value or score is included in errors."""


def require(condition: bool, description: str) -> None:
    if not condition:
        raise AuditBlocked(description)


def same(x: object, y: object, *, tolerance: float = 1e-10) -> bool:
    return (
        isinstance(x, (int, float)) and not isinstance(x, bool)
        and isinstance(y, (int, float)) and not isinstance(y, bool)
        and math.isfinite(x) and math.isfinite(y)
        and math.isclose(x, y, abs_tol=tolerance, rel_tol=tolerance)
    )


def finite(value: object, *, low: float | None = None,
           high: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return ((low is None or value >= low)
            and (high is None or value <= high))


def positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def validate_private_path(path: Path) -> None:
    require(path == FILE, "unexpected private analysis path")
    require(path.is_file() and not path.is_symlink(), "protected file absent or unsafe")
    info = path.stat()
    require(info.st_uid == os.getuid() and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
            and 0 < info.st_size < 20_000_000, "protected file metadata")
    root = path.parent
    require(root.is_dir() and not root.is_symlink()
            and stat.S_IMODE(root.stat().st_mode) == 0o700,
            "protected directory metadata")


def check_performance(data: dict, expected_n: int) -> None:
    require(isinstance(data, dict) and data.get("n") == expected_n,
            "performance denominator")
    events = data.get("events")
    require(type(events) is int and 0 < events < expected_n,
            "performance events")
    for metric in METRICS:
        require(finite(data.get(metric), low=0, high=1),
                "performance metric validity")
    require(same(data["prevalence"], events / expected_n),
            "prevalence denominator")
    for k in ("calibration_intercept", "calibration_slope"):
        require(data.get(k) is None or finite(data[k]),
                "calibration metric validity")


def check_importance(data: dict, features: list[str], expected_n: int,
                     limit: int) -> None:
    require(isinstance(data, dict)
            and data.get("permutation_evaluation_n") == expected_n,
            "full held-out permutation population")
    events = data.get("permutation_evaluation_events")
    require(type(events) is int and 0 < events < expected_n,
            "permutation events")
    shap_n = data.get("shap_evaluation_n")
    require(positive_int(shap_n) and shap_n <= min(limit, expected_n),
            "bounded SHAP evaluation")
    shap_events = data.get("shap_evaluation_events")
    require(type(shap_events) is int and 0 <= shap_events <= shap_n,
            "SHAP event validity")
    require(data.get("common_background_n") == 200
            and data.get("explanation_scale")
            == "positive-class predicted probability",
            "SHAP background or scale")
    scores = data.get("features")
    require(isinstance(scores, dict) and set(scores) == set(features),
            "importance feature coverage")
    for feature in features:
        value = scores[feature]
        require(isinstance(value, dict), "importance feature format")
        require(finite(value.get("permutation_auroc_decrease_mean"),
                       low=-1, high=1), "permutation mean range")
        require(finite(value.get("permutation_auroc_decrease_sd"), low=0),
                "permutation SD range")
        require(finite(value.get("mean_absolute_shap_probability"), low=0,
                       high=1), "SHAP probability range")
    for rank_name, key in (
        ("permutation_rank", "permutation_auroc_decrease_mean"),
        ("shap_rank", "mean_absolute_shap_probability"),
    ):
        ranks = data.get(rank_name)
        require(isinstance(ranks, list) and set(ranks) == set(features)
                and len(ranks) == len(features), "rank feature coverage")
        values = [scores[name][key] for name in ranks]
        require(all(values[i] + 1e-12 >= values[i+1]
                    for i in range(len(values) - 1)), "rank order mismatch")


def check_contrast(report: dict, left: dict, right: dict,
                   features: list[str]) -> None:
    require(isinstance(report, dict), "contrast format")
    require(report.get("left_permutation_evaluation_n")
            == left["permutation_evaluation_n"]
            and report.get("right_permutation_evaluation_n")
            == right["permutation_evaluation_n"], "contrast denominator")
    differences = report.get("feature_differences")
    require(isinstance(differences, dict)
            and set(differences) == set(features), "contrast feature coverage")
    for feature in features:
        observed = differences[feature]
        require(isinstance(observed, dict), "contrast feature format")
        for field, subfield in (
            ("permutation_auroc_decrease_difference",
             "permutation_auroc_decrease_mean"),
            ("mean_absolute_shap_probability_difference",
             "mean_absolute_shap_probability"),
        ):
            expected = right["features"][feature][subfield] - left["features"][feature][subfield]
            require(same(observed.get(field), expected), "contrast difference arithmetic")
        for field, obj, rank_type in (
            ("permutation_rank_left", left, "permutation_rank"),
            ("permutation_rank_right", right, "permutation_rank"),
            ("shap_rank_left", left, "shap_rank"),
            ("shap_rank_right", right, "shap_rank"),
        ):
            require(observed.get(field) == obj[rank_type].index(feature)+1,
                    "contrast rank arithmetic")


def audit(report: dict) -> None:
    require(isinstance(report, dict)
            and report.get("status")
            == "postfreeze_interpretability_completed_pending_disclosure_review",
            "run status")
    require(report.get("frozen_etl_sha") == m.FROZEN_SHA,
            "frozen ETL provenance")
    require(m.STUDY_PATH.is_file()
            and report.get("study_definition_sha256")
            == hashlib.sha256(m.STUDY_PATH.read_bytes()).hexdigest(),
            "locked protocol fingerprint")
    require(report.get("permutation_repeats") == 20
            and report.get("importance_max_shap_evaluation_n") == 500
            and report.get("permutation_test_scope") == "entire held-out population",
            "importance protocol settings")
    versions = report.get("software_versions")
    require(isinstance(versions, dict) and all(
        isinstance(versions.get(k), str) and versions[k]
        for k in ("python", "numpy", "pandas", "scikit-learn", "xgboost", "shap")
    ), "software provenance")
    anchors = report.get("population_anchors")
    require(isinstance(anchors, dict) and set(anchors) == set(m.EXPECTED),
            "population coverage")
    for group, (n, events) in m.EXPECTED.items():
        x = anchors[group]
        require(isinstance(x, dict)
                and x.get("n") == n and x.get("events") == events,
                "frozen 90-day population anchor")
        tr, ts = x.get("train_n"), x.get("test_n")
        require(positive_int(tr) and positive_int(ts) and tr + ts == n,
                "hash-split denominators")
    require(anchors["pcornet_full"]["test_n"] == anchors["omop_fallback"]["test_n"]
            and anchors["pcornet_shared"]["test_n"] == anchors["omop_frozen"]["test_n"],
            "paired held-out denominators")

    comparisons = report.get("comparisons")
    require(isinstance(comparisons, dict)
            and set(comparisons) == set(PAIRS)
            and all(comparisons[k] == list(v[:2]) for k, v in PAIRS.items()),
            "prespecified comparison pairs")

    models = report.get("models")
    contrasts = report.get("feature_importance_comparisons")
    require(isinstance(models, dict) and set(models) == set(SENSITIVITIES)
            and isinstance(contrasts, dict)
            and set(contrasts) == set(SENSITIVITIES),
            "sensitivity completeness")
    for sens, features in SENSITIVITIES.items():
        require(set(models[sens]) == set(MODELS)
                and set(contrasts[sens]) == set(MODELS), "model coverage")
        for model in MODELS:
            groups = models[sens][model]
            contrast_group = contrasts[sens][model]
            require(set(groups) == set(m.EXPECTED)
                    and set(contrast_group) == set(PAIRS), "group coverage")
            common_n = anchors["pcornet_shared"]["test_n"]
            common_events = set()
            for group in m.EXPECTED:
                data = groups[group]
                require(isinstance(data, dict), "model result format")
                native_n = anchors[group]["test_n"]
                for scope, expected_n in (
                    ("native_test", native_n),
                    ("common_shared_test", common_n),
                ):
                    perf = data.get(scope + "_performance")
                    importance = data.get(scope + "_importance")
                    check_performance(perf, expected_n)
                    check_importance(importance, features, expected_n, 500)
                    require(importance["permutation_evaluation_events"]
                            == perf["events"], "importance/performance label agreement")
                    if scope == "common_shared_test":
                        common_events.add(perf["events"])
            require(len(common_events) == 1,
                    "common held-out outcome agreement")
            for label, (left_name, right_name, scope) in PAIRS.items():
                comparison = contrast_group[label]
                require(comparison.get("left") == left_name
                        and comparison.get("right") == right_name
                        and comparison.get("evaluation_scope") == scope,
                        "comparison scope")
                check_contrast(
                    comparison,
                    groups[left_name][scope + "_importance"],
                    groups[right_name][scope + "_importance"],
                    features,
                )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Private RF/XGBoost aggregate audit; prints no scores"
    )
    p.add_argument("--input", default=str(FILE))
    args = p.parse_args()
    try:
        candidate = Path(os.path.abspath(args.input))
        validate_private_path(candidate)
        with candidate.open("r", encoding="utf-8") as stream:
            report = json.load(
                stream,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    AuditBlocked("nonfinite JSON numeric")
                ),
            )
        audit(report)
    except Exception:
        # Do not print exception strings or traceback; private JSON can contain
        # institution-only aggregate and model information.
        print("RF XGBOOST PRIVATE AGGREGATE AUDIT: BLOCKED", file=sys.stderr)
        raise SystemExit(1) from None
    print("RF XGBOOST PRIVATE AGGREGATE AUDIT: PASS")
    print("Completed model/sensitivity/population matrix: PASS")
    print("Held-out performance and importance scope: PASS")
    print("SHAP/feature ranking, contrast arithmetic and provenance: PASS")
    print("Protected private output retained locally; no data exported.")
    print("Scientific stability and institutional disclosure review still required.")


if __name__ == "__main__":
    main()
