from __future__ import annotations

"""Exploratory post-freeze RF/XGBoost interpretability across four locked populations.

All inputs are RESTRICTED, row-level local Parquet files. Only disclosure-reviewed,
aggregate JSON is written. This module does not modify the frozen ETL or original
Stage E analyses. Data extraction is a separate, reviewed prerequisite.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
FEATURES = [
    "age_at_index_years",
    "female_indicator",
    "index_length_of_stay_days",
    "prior_365d_acute_care_encounter_count",
    "prior_365d_all_encounter_count",
    "prior_365d_ischemic_stroke_indicator",
]
CONTINUOUS = [
    "age_at_index_years", "index_length_of_stay_days",
    "prior_365d_acute_care_encounter_count", "prior_365d_all_encounter_count",
]
BINARY = ["female_indicator", "prior_365d_ischemic_stroke_indicator"]
EXPECTED = {
    "pcornet_full": (6508, 1798),
    "pcornet_shared": (3822, 1132),
    "omop_frozen": (3822, 1132),
    "omop_fallback": (6508, 1798),
}
SEED = 20261029
STUDY_PATH = Path("study_definitions/postfreeze_standard_qc_interpretability_v1.json")


ROOT = Path(__file__).resolve().parents[3]
PRIVATE_INPUT_ROOT = ROOT / "results/private_inputs"
PRIVATE_RESULTS_ROOT = ROOT / "results/postfreeze_interpretability"


def _private_path_under(root: Path, raw: str | Path) -> Path:
    """Reject outside paths and symlinks before opening protected files."""
    candidate = Path(os.path.abspath(Path(raw).expanduser()))
    safe_root = Path(os.path.abspath(root))
    if candidate == safe_root or not candidate.is_relative_to(safe_root):
        raise ValueError("Protected file path escapes approved ignored results directory")
    cursor = safe_root
    if cursor.is_symlink():
        raise ValueError("Protected file path includes symlink")
    for part in candidate.relative_to(safe_root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Protected file path includes symlink")
    if not candidate.resolve().is_relative_to(safe_root.resolve()):
        raise ValueError("Protected file path escapes ignored results directory")
    return candidate


def validate_private_input_path(path: str, population: str) -> Path:
    """Require the exact exported file under a protected 0700 directory."""
    candidate = _private_path_under(PRIVATE_INPUT_ROOT, path)
    if candidate != PRIVATE_INPUT_ROOT / f"{population}.parquet":
        raise ValueError("Input must be the matching locked private cohort Parquet")
    folder = candidate.parent
    if not folder.is_dir() or stat.S_IMODE(folder.stat().st_mode) != 0o700:
        raise ValueError("Private input directory must have mode 0700")
    try:
        info = candidate.lstat()
    except OSError:
        raise ValueError("Protected input Parquet not found") from None
    if (not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or not 0 < info.st_size <= 100_000_000):
        raise ValueError("Protected input Parquet has unsafe file metadata")
    return candidate


def validate_private_output_path(path: str) -> Path:
    candidate = _private_path_under(PRIVATE_RESULTS_ROOT, path)
    if candidate.name != "aggregate_results.json" or candidate.parent != PRIVATE_RESULTS_ROOT:
        raise ValueError("Output must be the locked aggregate result filename")
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Aggregate output already exists; preserve prior analysis")
    if PRIVATE_RESULTS_ROOT.exists():
        if (not PRIVATE_RESULTS_ROOT.is_dir()
                or stat.S_IMODE(PRIVATE_RESULTS_ROOT.stat().st_mode) != 0o700):
            raise ValueError("Aggregate output directory must have mode 0700")
    return candidate


def write_private_aggregate(path: Path, result: dict) -> None:
    """Write create-only owner-readable result pending institutional review."""
    validated = validate_private_output_path(str(path))
    validated.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(validated.parent.stat().st_mode) != 0o700:
        raise ValueError("Aggregate output directory must have mode 0700")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(validated, flags, 0o600), "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if stat.S_IMODE(validated.stat().st_mode) != 0o600:
        raise ValueError("Aggregate file must have mode 0600")



def is_train(person: object) -> bool:
    """Identical patient-level split algorithm to locked Stage E."""
    digest = hashlib.sha256(str(person).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10 < 7


def read_population(path: str, population: str) -> pd.DataFrame:
    candidate = validate_private_input_path(path, population)
    frame = pd.read_parquet(candidate)
    required = {"patid", "index_date", "outcome", *FEATURES}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{population}: missing columns {sorted(missing)}")
    frame = frame[["patid", "index_date", "outcome", *FEATURES]].copy()
    if frame["patid"].isna().any():
        raise ValueError(f"{population}: null patient keys")
    frame["patid"] = frame["patid"].astype(str)
    if frame.patid.duplicated().any():
        raise ValueError(f"{population}: multiple rows per patient")
    index = pd.to_datetime(frame["index_date"], errors="coerce")
    if index.isna().any():
        raise ValueError(f"{population}: null or invalid index dates")
    frame["index_date"] = index.dt.strftime("%Y-%m-%d")
    frame["outcome"] = pd.to_numeric(frame["outcome"], errors="coerce")
    if frame["outcome"].isna().any() or not frame["outcome"].isin([0, 1]).all():
        raise ValueError(f"{population}: non-binary outcome")
    frame["outcome"] = frame["outcome"].astype(int)
    for feature in FEATURES:
        frame[feature] = pd.to_numeric(frame[feature], errors="raise")
        if not np.isfinite(frame[feature].dropna().to_numpy(dtype=float)).all():
            raise ValueError(f"{population}: non-finite predictor value")
    for feature in BINARY:
        if not frame[feature].dropna().isin([0, 1]).all():
            raise ValueError(f"{population}: non-binary {feature}")
    expected_n, expected_events = EXPECTED[population]
    if len(frame) != expected_n or int(frame.outcome.sum()) != expected_events:
        raise ValueError(f"{population}: Stage D anchor count/event mismatch")
    frame["is_train"] = frame.patid.map(is_train)
    return frame.set_index("patid", drop=False).sort_index()


def assert_pair(a: pd.DataFrame, b: pd.DataFrame, label: str) -> None:
    if set(a.index) != set(b.index):
        raise ValueError(f"{label}: patient membership differs")
    paired = a.loc[b.index]
    if not (paired.index_date == b.index_date).all():
        raise ValueError(f"{label}: index date mismatch")
    if not (paired.outcome == b.outcome).all():
        raise ValueError(f"{label}: 90-day outcome mismatch")
    if not (paired.is_train == b.is_train).all():
        raise ValueError(f"{label}: train/test assignment mismatch")


def validate_populations(populations: dict[str, pd.DataFrame]) -> None:
    source, shared, frozen, fallback = (populations[k] for k in EXPECTED)
    if not set(shared.index).issubset(source.index):
        raise ValueError("Shared source population must be a source subset")
    assert_pair(source.loc[shared.index], shared, "Within-PCORnet shared subset")
    for feature in FEATURES:
        x = source.loc[shared.index, feature].to_numpy(dtype=float)
        y = shared[feature].to_numpy(dtype=float)
        if not np.allclose(x, y, equal_nan=True, rtol=0, atol=0):
            raise ValueError(f"Within-PCORnet feature {feature} changed on restriction")
    assert_pair(shared, frozen, "Fixed population PCORnet/OMOP")
    assert_pair(source, fallback, "Full population PCORnet/fallback OMOP")
    for key, data in populations.items():
        if data.is_train.nunique() < 2:
            raise ValueError(f"{key}: invalid train/test assignment")
        if data.loc[data.is_train, "outcome"].nunique() != 2:
            raise ValueError(f"{key}: training data missing outcome class")
        if data.loc[~data.is_train, "outcome"].nunique() != 2:
            raise ValueError(f"{key}: test data missing outcome class")


def imputation_values(train: pd.DataFrame, features: list[str]) -> dict[str, float]:
    vals: dict[str, float] = {}
    for feature in features:
        observed = train[feature].dropna()
        if feature in BINARY:
            mode = observed.mode()
            vals[feature] = float(mode.iloc[0]) if len(mode) else 0.0
        else:
            vals[feature] = float(observed.median()) if len(observed) else 0.0
    return vals


def filled(frame: pd.DataFrame, features: list[str], values: dict[str, float]) -> pd.DataFrame:
    return frame[features].fillna(values).astype(float)


def make_model(name: str):
    if name == "random_forest":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(
            n_estimators=300, min_samples_leaf=10, max_features="sqrt",
            random_state=SEED, n_jobs=1,
        )
    if name == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=300, max_depth=3, learning_rate=0.05,
            subsample=1.0, colsample_bytree=1.0, objective="binary:logistic",
            eval_metric="logloss", tree_method="hist", random_state=SEED, n_jobs=1,
        )
    raise ValueError(f"Unexpected model: {name}")


def performance(y: np.ndarray, probability: np.ndarray) -> dict[str, float | int | None]:
    from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss
    from sklearn.linear_model import LogisticRegression
    p = np.clip(probability, 1e-6, 1 - 1e-6)
    logit = np.log(p / (1 - p)).reshape(-1, 1)
    intercept, slope = None, None
    if len(np.unique(y)) == 2 and float(np.std(logit)) > 0:
        calibration = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
        calibration.fit(logit, y)
        intercept = float(calibration.intercept_[0])
        slope = float(calibration.coef_[0][0])
    return {
        "n": int(len(y)),
        "events": int(np.sum(y)),
        "prevalence": float(np.mean(y)),
        "auroc": float(roc_auc_score(y, probability)),
        "auprc": float(average_precision_score(y, probability)),
        "brier": float(brier_score_loss(y, probability)),
        "calibration_intercept": intercept,
        "calibration_slope": slope,
    }


def evaluation_sample(data: pd.DataFrame, limit: int) -> pd.DataFrame:
    # Stable, bounded set for interpretation; performance uses the full held-out set.
    subset = data[~data.is_train].sort_index()
    if len(subset) > limit:
        subset = subset.sample(n=limit, random_state=SEED)
    if subset.outcome.nunique() != 2:
        raise ValueError("Interpretability sample has only one outcome class")
    return subset


def attribution(model, features: list[str], test: pd.DataFrame,
                background: pd.DataFrame, impute: dict[str, float],
                repeats: int, shap_sample: pd.DataFrame | None = None) -> dict:
    """Full held-out AUROC permutation; bounded SHAP explanation sample."""
    from sklearn.inspection import permutation_importance
    import shap

    if test.outcome.nunique() != 2:
        raise ValueError("Permutation evaluation requires both outcome classes")
    if shap_sample is None:
        shap_sample = test
    if not set(shap_sample.index).issubset(test.index):
        raise ValueError("SHAP sample must be a subset of held-out evaluation")
    perm_x = filled(test, features, impute)
    perm_y = test.outcome.to_numpy(dtype=int)
    perm = permutation_importance(
        model, perm_x, perm_y, n_repeats=repeats, random_state=SEED,
        n_jobs=1, scoring="roc_auc",
    )
    # Common source-reference rows across cohorts, with population-specific
    # imputation fitted on training data only.
    bg = filled(background, features, impute)
    shap_x = filled(shap_sample, features, impute)
    explainer = shap.TreeExplainer(
        model, data=bg, feature_perturbation="interventional",
        model_output="probability",
    )
    vals = explainer.shap_values(shap_x, check_additivity=False)
    if isinstance(vals, list):
        vals = vals[1]
    vals = np.asarray(vals)
    if vals.ndim == 3 and vals.shape[2] == 2:
        vals = vals[:, :, 1]
    if vals.shape != (len(shap_x), len(features)):
        raise RuntimeError(f"Unexpected positive-class SHAP shape {vals.shape}")
    abs_mean = np.mean(np.abs(vals), axis=0)
    if not (np.isfinite(perm.importances_mean).all()
            and np.isfinite(perm.importances_std).all()
            and np.isfinite(abs_mean).all()):
        raise RuntimeError("Non-finite feature-importance statistics")
    return {
        "permutation_evaluation_n": int(len(test)),
        "permutation_evaluation_events": int(perm_y.sum()),
        "shap_evaluation_n": int(len(shap_sample)),
        "shap_evaluation_events": int(shap_sample.outcome.sum()),
        "explanation_scale": "positive-class predicted probability",
        "common_background_n": int(len(bg)),
        "features": {
            feature: {
                "permutation_auroc_decrease_mean": float(perm.importances_mean[i]),
                "permutation_auroc_decrease_sd": float(perm.importances_std[i]),
                "mean_absolute_shap_probability": float(abs_mean[i]),
            }
            for i, feature in enumerate(features)
        },
        "shap_rank": sorted(features, key=lambda f: -float(abs_mean[features.index(f)])),
        "permutation_rank": sorted(features, key=lambda f: -float(perm.importances_mean[features.index(f)])),
    }


def importance_difference(left: dict, right: dict) -> dict:
    """Descriptive right-minus-left importance/rank contrasts, never causal."""
    feature_names = list(left["features"])
    if set(feature_names) != set(right["features"]):
        raise ValueError("Feature sets differ between importance results")
    out: dict[str, dict] = {}
    for feature in feature_names:
        l, r = left["features"][feature], right["features"][feature]
        out[feature] = {
            "permutation_auroc_decrease_difference": float(
                r["permutation_auroc_decrease_mean"] -
                l["permutation_auroc_decrease_mean"]
            ),
            "mean_absolute_shap_probability_difference": float(
                r["mean_absolute_shap_probability"] -
                l["mean_absolute_shap_probability"]
            ),
            "permutation_rank_left": left["permutation_rank"].index(feature) + 1,
            "permutation_rank_right": right["permutation_rank"].index(feature) + 1,
            "shap_rank_left": left["shap_rank"].index(feature) + 1,
            "shap_rank_right": right["shap_rank"].index(feature) + 1,
        }
    return {
        "left_permutation_evaluation_n": left["permutation_evaluation_n"],
        "right_permutation_evaluation_n": right["permutation_evaluation_n"],
        "feature_differences": out,
        "interpretation": (
            "Descriptive differences only. They mix model fitting, source "
            "representation, population and correlated feature effects."
        ),
    }


def common_shap_background(shared: pd.DataFrame, limit: int = 200) -> pd.DataFrame:
    """Common source training background sampled by locked fixed random seed."""
    if limit < 1:
        raise ValueError("SHAP background limit must be positive")
    training = shared[shared.is_train].sort_index()
    if training.empty:
        raise ValueError("Shared training population is empty")
    return training.sample(n=min(limit, len(training)), random_state=SEED)


def analyze(populations: dict[str, pd.DataFrame], max_eval: int = 500,
            repeats: int = 20) -> dict:
    if max_eval < 50 or repeats < 2:
        raise ValueError("max_eval must be >=50 and repeats >=2")
    validate_populations(populations)
    shared = populations["pcornet_shared"]
    # Permutation importance and performance use ALL matched shared held-out
    # patients. Only SHAP is capped for computational feasibility.
    common_ids = shared.loc[~shared.is_train].sort_index().index
    common_shap_ids = evaluation_sample(shared, max_eval).index
    common_bg = common_shap_background(shared)
    output: dict = {
        "status": "postfreeze_interpretability_completed_pending_disclosure_review",
        "frozen_etl_sha": FROZEN_SHA,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "population_anchors": {
            name: {"n": int(len(frame)), "events": int(frame.outcome.sum()),
                   "train_n": int(frame.is_train.sum()),
                   "test_n": int((~frame.is_train).sum())}
            for name, frame in populations.items()
        },
        "comparisons": {
            "end_to_end": ["pcornet_full", "omop_frozen"],
            "selection_within_pcornet": ["pcornet_full", "pcornet_shared"],
            "representation_fixed": ["pcornet_shared", "omop_frozen"],
            "representation_fallback": ["pcornet_full", "omop_fallback"],
        },
        "models": {},
        "interpretation_guardrails": [
            "Model attributions are not causal risk-factor effects.",
            "Different native evaluation populations can change importance independently of fitting.",
            "Primary permutation importance uses full held-out sets; SHAP uses up to max_eval held-out patients.",
            "Within-PCORnet selection and frozen representation use matched shared test patients.",
            "SHAP uses one source-reference background with model-specific training-only imputations.",
            "Prior-stroke history is diagnosis-date-ascertainment-sensitive.",
            "Prior-encounter variables are correlated; individual importance may be shared.",
            "No row-level data, predictions or identifiers are written.",
        ],
    }
    for sensitivity, features in [
        ("locked_six_features", FEATURES),
        ("drop_date_sensitive_prior_stroke", [f for f in FEATURES if f != "prior_365d_ischemic_stroke_indicator"]),
    ]:
        output["models"][sensitivity] = {}
        for model_name in ("random_forest", "xgboost"):
            output["models"][sensitivity][model_name] = {}
            for group, frame in populations.items():
                train = frame[frame.is_train]
                test = frame[~frame.is_train]
                imputes = imputation_values(train, features)
                model = make_model(model_name)
                model.fit(
                    filled(train, features, imputes),
                    train.outcome.to_numpy(dtype=int),
                )
                native_prob = model.predict_proba(filled(test, features, imputes))[:, 1]
                matched_test = frame.loc[common_ids]
                matched_prob = model.predict_proba(
                    filled(matched_test, features, imputes)
                )[:, 1]
                output["models"][sensitivity][model_name][group] = {
                    "native_test_performance": performance(test.outcome.to_numpy(dtype=int), native_prob),
                    "common_shared_test_performance": performance(
                        matched_test.outcome.to_numpy(dtype=int), matched_prob
                    ),
                    "native_test_importance": attribution(
                        model, features, test,
                        common_bg, imputes, repeats,
                        shap_sample=evaluation_sample(frame, max_eval),
                    ),
                    "common_shared_test_importance": attribution(
                        model, features, matched_test, common_bg,
                        imputes, repeats,
                        shap_sample=frame.loc[common_shap_ids],
                    ),
                }
                print(f"completed {sensitivity}/{model_name}/{group}", flush=True)
            group_out = output["models"][sensitivity][model_name]
            output.setdefault("feature_importance_comparisons", {}).setdefault(
                sensitivity, {}
            )[model_name] = {}
            for label, (left_name, right_name) in output["comparisons"].items():
                # Contrasts on separately eligible native test cohorts are the
                # direct end-to-end question. Common tests isolate evaluation
                # population differences but models are still fitted separately.
                scope = (
                    "native_test" if label in (
                        "end_to_end", "representation_fallback",
                    ) else "common_shared_test"
                )
                suffix = scope + "_importance"
                output["feature_importance_comparisons"][sensitivity][model_name][label] = {
                    "left": left_name,
                    "right": right_name,
                    "evaluation_scope": scope,
                    **importance_difference(
                        group_out[left_name][suffix],
                        group_out[right_name][suffix],
                    ),
                }
    return output


def main() -> None:
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("status") != "proposed_postfreeze_protocol_not_executed":
        raise ValueError("Post-freeze protocol status changed unexpectedly")
    if study.get("frozen_etl_sha") != FROZEN_SHA:
        raise ValueError("Frozen ETL SHA mismatch")
    if study["interpretability"]["features"] != FEATURES:
        raise ValueError("Post-freeze protocol feature list mismatch")
    if {entry["key"]: entry["expected_n"] for entry in study["interpretability"]["populations"]} != {
        key: EXPECTED[key][0] for key in EXPECTED
    }:
        raise ValueError("Post-freeze protocol population sizes mismatch")
    p = argparse.ArgumentParser(description="Exploratory post-freeze RF/XGBoost SHAP analysis")
    for key in EXPECTED:
        p.add_argument("--" + key.replace("_", "-"), required=True,
                       help="Restricted local Parquet input; never commit these files")
    p.add_argument("--output", default="results/postfreeze_interpretability/aggregate_results.json")
    p.add_argument("--max-eval", type=int, default=500)
    p.add_argument("--permutation-repeats", type=int, default=20)
    p.add_argument("--validate-only", action="store_true",
                   help="Check all protected input cohorts and output path without fitting")
    args = p.parse_args()
    try:
        output_path = validate_private_output_path(args.output)
        populations = {
            key: read_population(getattr(args, key), key) for key in EXPECTED
        }
        validate_populations(populations)
        if args.validate_only:
            print("RF XGBOOST PRIVATE INPUT VALIDATION: PASS")
            print("Locked cohorts, 90-day labels, index dates and patient split: PASS")
            print("No model fitted, no files written.")
            return

        result = analyze(populations, args.max_eval, args.permutation_repeats)
        result["study_definition_sha256"] = hashlib.sha256(STUDY_PATH.read_bytes()).hexdigest()
        result["software_versions"] = {
            "python": sys.version.split()[0],
            **{package: importlib.metadata.version(package)
               for package in ("numpy", "pandas", "scikit-learn", "xgboost", "shap")},
        }
        result["permutation_repeats"] = args.permutation_repeats
        result["importance_max_shap_evaluation_n"] = args.max_eval
        result["permutation_test_scope"] = "entire held-out population"
        write_private_aggregate(output_path, result)
        print("RF XGBOOST PRIVATE AGGREGATE ANALYSIS: COMPLETE")
        print("Output remains local and requires institutional disclosure review.")
        print("No patient identifiers or predictions were exported.")
    except Exception:
        # Never reveal row-level data, filesystem identifiers, or model errors
        # from the institutional input path in unreviewed terminal logs.
        print(
            "RF XGBOOST PRIVATE ANALYSIS BLOCKED: sensitive diagnostics "
            "suppressed. Preserve all local files for restricted review.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
