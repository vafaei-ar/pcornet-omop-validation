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


def is_train(person: object) -> bool:
    """Identical patient-level split algorithm to locked Stage E."""
    digest = hashlib.sha256(str(person).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10 < 7


def read_population(path: str, population: str) -> pd.DataFrame:
    if not str(path).lower().endswith(".parquet"):
        raise ValueError("Private population inputs must be local Parquet files")
    frame = pd.read_parquet(path)
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
    for feature in BINARY:
        if not frame[feature].dropna().isin([0, 1]).all():
            raise ValueError(f"{population}: non-binary {feature}")
    expected_n, expected_events = EXPECTED[population]
    if len(frame) != expected_n or int(frame.outcome.sum()) != expected_events:
        raise ValueError(f"{population}: Stage D anchor count/event mismatch")
    frame["is_train"] = frame.patid.map(is_train)
    return frame.set_index("patid", drop=False)


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


def performance(y: np.ndarray, probability: np.ndarray) -> dict[str, float | int]:
    from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss
    return {
        "n": int(len(y)),
        "events": int(np.sum(y)),
        "prevalence": float(np.mean(y)),
        "auroc": float(roc_auc_score(y, probability)),
        "auprc": float(average_precision_score(y, probability)),
        "brier": float(brier_score_loss(y, probability)),
    }


def evaluation_sample(data: pd.DataFrame, limit: int) -> pd.DataFrame:
    # Stable, bounded set for interpretation; performance uses the full held-out set.
    subset = data[~data.is_train]
    if len(subset) > limit:
        subset = subset.sample(n=limit, random_state=SEED)
    if subset.outcome.nunique() != 2:
        raise ValueError("Interpretability sample has only one outcome class")
    return subset


def attribution(model, features: list[str], test: pd.DataFrame,
                background: pd.DataFrame, impute: dict[str, float],
                repeats: int) -> dict:
    from sklearn.inspection import permutation_importance
    import shap

    x = filled(test, features, impute)
    y = test.outcome.to_numpy(dtype=int)
    perm = permutation_importance(
        model, x, y, n_repeats=repeats, random_state=SEED,
        n_jobs=1, scoring="roc_auc",
    )
    # Background is a common source-based reference for all four populations.
    bg = filled(background, features, impute)
    explainer = shap.TreeExplainer(
        model, data=bg, feature_perturbation="interventional",
        model_output="probability",
    )
    vals = explainer.shap_values(x, check_additivity=False)
    if isinstance(vals, list):
        vals = vals[1]  # positive class
    vals = np.asarray(vals)
    if vals.ndim == 3 and vals.shape[2] == 2:
        vals = vals[:, :, 1]
    if vals.shape != (len(x), len(features)):
        raise RuntimeError(f"Unexpected positive-class SHAP shape {vals.shape}")
    abs_mean = np.mean(np.abs(vals), axis=0)
    return {
        "evaluation_n": int(len(test)),
        "evaluation_events": int(y.sum()),
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


def analyze(populations: dict[str, pd.DataFrame], max_eval: int = 500,
            repeats: int = 20) -> dict:
    if max_eval < 50 or repeats < 2:
        raise ValueError("max_eval must be >=50 and repeats >=2")
    validate_populations(populations)
    shared = populations["pcornet_shared"]
    # Common identical patient set: all four groups are evaluated on these IDs.
    common_ids = evaluation_sample(shared, max_eval).index
    common_bg = shared[shared.is_train].sort_index().head(200)
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
            "selection_within_pcornet": ["pcornet_full", "pcornet_shared"],
            "representation_fixed": ["pcornet_shared", "omop_frozen"],
            "representation_fallback": ["pcornet_full", "omop_fallback"],
        },
        "models": {},
        "interpretation_guardrails": [
            "Model attributions are not causal risk-factor effects.",
            "Different native evaluation populations can change importance independently of fitting.",
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
                        model, features, evaluation_sample(frame, max_eval),
                        common_bg, imputes, repeats,
                    ),
                    "common_shared_test_importance": attribution(
                        model, features, matched_test, common_bg,
                        imputes, repeats,
                    ),
                }
                print(f"completed {sensitivity}/{model_name}/{group}", flush=True)
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
    args = p.parse_args()
    populations = {
        key: read_population(getattr(args, key), key) for key in EXPECTED
    }
    result = analyze(populations, args.max_eval, args.permutation_repeats)
    result["study_definition_sha256"] = hashlib.sha256(STUDY_PATH.read_bytes()).hexdigest()
    result["software_versions"] = {
        "python": sys.version.split()[0],
        **{package: importlib.metadata.version(package)
           for package in ("numpy", "pandas", "scikit-learn", "xgboost", "shap")},
    }
    result["permutation_repeats"] = args.permutation_repeats
    result["importance_max_evaluation_n"] = args.max_eval
    path = Path(args.output)
    if "results" not in path.parts:
        raise ValueError("Write only to ignored local results/ directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("Aggregate results require institutional disclosure review:", path)


if __name__ == "__main__":
    main()
