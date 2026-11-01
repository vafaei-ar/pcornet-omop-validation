"""Private RF/XGBoost training-perturbation and feature-correlation review.

This extension executes ONLY on institutional, ignored, protected local
Parquet cohorts. It does not query SQL, alter frozen CDM/Stage E, reuse or
overwrite the original attribution result, or print private numbers.
The result is exploratory. Five fits (one full train, four deterministic
90% patient-ID-aligned subsets) are NOT a patient-bootstrap CI or p-value.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import numpy as np
import pandas as pd

from pcornet_omop_validation.study import (
    postfreeze_model_interpretability as m,
    postfreeze_model_interpretability_audit as audit,
)

DESTINATION = m.PRIVATE_RESULTS_ROOT / "private_stability_review.md"
REPLICATES = 5  # baseline + four training perturbations, not inferential trials
RETAIN_TRAINING = 0.90
PERM_REPEATS = 3  # training robustness probe, not main 20-repeat estimand
CORR_THRESHOLD = 0.70


def training_subset(data: pd.DataFrame, replicate: int) -> pd.DataFrame:
    """Select the same patient IDs across paired model representations."""
    if replicate not in range(REPLICATES):
        raise ValueError("Invalid prespecified training perturbation")
    training = data.loc[data.is_train].sort_index()
    if replicate == 0:
        return training
    masks = [
        int.from_bytes(
            hashlib.sha256(
                f"training-perturbation-v1:{m.SEED}:{replicate}:{identifier}".encode()
            ).digest()[:8], "big"
        ) / (2**64) < RETAIN_TRAINING
        for identifier in training.index
    ]
    selected = training.loc[np.asarray(masks, dtype=bool)]
    if selected.outcome.nunique() != 2:
        raise ValueError("Training perturbation lost an outcome class")
    return selected


def _render_numeric(value: object) -> str:
    if value is None or not np.isfinite(float(value)):
        return "undefined"
    return f"{float(value):+.5f}"


def predictor_diagnostics(populations: dict[str, pd.DataFrame]) -> list[str]:
    """Private-only missingness, correlations and paired feature disagreement."""
    lines = [
        "## Input predictor diagnostics",
        "",
        "Train-only missingness and Spearman correlations are descriptive. "
        "Correlations become undefined when a predictor is constant. "
        "The threshold |rho| >= 0.70 is a descriptive screening flag, "
        "not a predeclared inferential cutoff.",
        "",
        "| Population | Predictor | Train missing proportion |",
        "| --- | --- | ---: |",
    ]
    for group, frame in populations.items():
        train = frame.loc[frame.is_train]
        for name in m.FEATURES:
            lines.append(
                f"| {group} | `{name}` | "
                f"{float(train[name].isna().mean()):.4f} |"
            )
    lines.extend(["", "### Correlated predictors", ""])
    for group, frame in populations.items():
        corr = frame.loc[frame.is_train, m.FEATURES].corr(method="spearman")
        lines.extend([f"#### {group}", "",
                      "| Predictor A | Predictor B | Spearman rho | Absolute rho >=0.70 |",
                      "| --- | --- | ---: | :---: |"])
        for i, a in enumerate(m.FEATURES):
            for b in m.FEATURES[i+1:]:
                value = corr.loc[a,b]
                flag = "Yes" if pd.notna(value) and abs(value) >= CORR_THRESHOLD else (
                    "Undefined" if pd.isna(value) else "No"
                )
                lines.append(f"| `{a}` | `{b}` | {_render_numeric(value)} | {flag} |")
        lines.append("")
    lines.extend([
        "### Paired source-target feature disagreement",
        "",
        "Counts below are restricted derived clinical aggregates and must not "
        "be copied into public or conversational output without approval.",
        "",
        "| Matched population pair | Feature | Disagreeing values | Fraction |",
        "| --- | --- | ---: | ---: |",
    ])
    for left_name, right_name in (
        ("pcornet_shared","omop_frozen"),
        ("pcornet_full","omop_fallback"),
    ):
        left = populations[left_name]
        right = populations[right_name].loc[left.index]
        for feature in m.FEATURES:
            x = left[feature].to_numpy(dtype=float)
            y = right[feature].to_numpy(dtype=float)
            matches = np.isclose(x,y,rtol=0,atol=1e-8,equal_nan=True)
            n_changed = int((~matches).sum())
            lines.append(
                f"| {left_name} vs {right_name} | `{feature}` | "
                f"{n_changed} | {n_changed / len(matches):.4f} |"
            )
    return lines


def _model_permutation(model, train: pd.DataFrame, test: pd.DataFrame,
                       features: list[str]) -> dict[str, float]:
    """Full held-out AUROC permutation with fixed shuffle seed."""
    from sklearn.inspection import permutation_importance

    impute = m.imputation_values(train, features)
    model.fit(m.filled(train,features,impute),train.outcome.to_numpy(dtype=int))
    results = {}
    # Stable held-out patient order, identical across paired data models.
    for scope, subset in test.items():
        if subset.outcome.nunique() != 2:
            raise ValueError("Robustness test population has one outcome class")
        perm = permutation_importance(
            model, m.filled(subset,features,impute),
            subset.outcome.to_numpy(dtype=int),n_repeats=PERM_REPEATS,
            random_state=m.SEED,n_jobs=1,scoring="roc_auc",
        )
        means = perm.importances_mean
        if not np.isfinite(means).all():
            raise RuntimeError("Non-finite exploratory permutation score")
        results[scope] = dict(zip(features,map(float,means), strict=True))
    return results


def compute_seed_stability(
    populations: dict[str, pd.DataFrame],
) -> dict[str, dict]:
    """Keep only per-feature aggregate attributions in memory, not predictions."""
    m.validate_populations(populations)
    shared_ids = populations["pcornet_shared"].loc[
        ~populations["pcornet_shared"].is_train
    ].sort_index().index
    results = {}
    for sensitivity, features in audit.SENSITIVITIES.items():
        results[sensitivity] = {}
        for algorithm in audit.MODELS:
            results[sensitivity][algorithm] = {}
            for group, frame in populations.items():
                native = frame.loc[~frame.is_train].sort_index()
                common = frame.loc[shared_ids]
                trials = []
                for rep in range(REPLICATES):
                    training = training_subset(frame, rep)
                    model = m.make_model(algorithm)
                    model.set_params(random_state=m.SEED + rep)
                    trial = _model_permutation(
                        model, training,
                        {"native_test": native, "common_shared_test": common},
                        features,
                    )
                    trials.append(trial)
                results[sensitivity][algorithm][group] = trials
    return results


def _rank(scores: dict[str,float]) -> list[str]:
    return sorted(scores, key=lambda k: (-scores[k],k))


def _sign(x: float, tol: float = 1e-7) -> int:
    return 1 if x > tol else (-1 if x < -tol else 0)


def render_stability(
    trials: dict[str,dict],
    original_report: dict,
) -> list[str]:
    lines = [
        "## Training perturbation robustness",
        "",
        f"Each algorithm/data-model/feature-set combination was refit "
        f"{REPLICATES} times: once on the full locked training partition "
        f"and {REPLICATES-1} times on reproducible ~90% training subsets. "
        "Patient-ID hashing aligns training subset inclusion across matched "
        "source and target representations. The original held-out sets "
        "and split assignments remain unchanged.",
        "",
        f"Each refit uses {PERM_REPEATS} fixed-seed shuffles for held-out "
        "AUROC permutation importance. These values are exploratory "
        "sensitivity results, not a reproduction of the locked 20-repeat "
        "single-fit importance estimates. Do not use their ranges as "
        "confidence intervals or infer causal significance.",
        "",
    ]
    for sensitivity, features in audit.SENSITIVITIES.items():
        lines.extend([f"### {sensitivity}", ""])
        for label,(left,right,scope) in audit.PAIRS.items():
            lines.extend([
                f"#### {label}: {left} vs {right} ({scope})",
                "",
                "| Model | Feature | Delta baseline | Min delta across fits | "
                "Max delta across fits | Positive / Zero / Negative fit counts | "
                "Left rank baseline to range | Right rank baseline to range |",
                "| --- | --- | ---: | ---: | ---: | :---: | :---: | :---: |",
            ])
            for algorithm in audit.MODELS:
                left_trials = trials[sensitivity][algorithm][left]
                right_trials = trials[sensitivity][algorithm][right]
                original_groups = original_report["models"][sensitivity][algorithm]
                original_left = original_groups[left][scope+"_importance"]
                original_right = original_groups[right][scope+"_importance"]
                for feature in features:
                    delta = [
                        r[scope][feature] - l[scope][feature]
                        for l,r in zip(left_trials,right_trials,strict=True)
                    ]
                    signs = [_sign(x) for x in delta]
                    left_rank = [_rank(x[scope]).index(feature)+1 for x in left_trials]
                    right_rank = [_rank(x[scope]).index(feature)+1 for x in right_trials]
                    left_baseline = original_left["permutation_rank"].index(feature)+1
                    right_baseline = original_right["permutation_rank"].index(feature)+1
                    lines.append(
                        f"| {algorithm} | `{feature}` | "
                        f"{_render_numeric(delta[0])} | "
                        f"{_render_numeric(min(delta))} | "
                        f"{_render_numeric(max(delta))} | "
                        f"{signs.count(1)} / {signs.count(0)} / {signs.count(-1)} | "
                        f"{left_baseline} to {min(left_rank)}-{max(left_rank)} | "
                        f"{right_baseline} to {min(right_rank)}-{max(right_rank)} |"
                    )
            lines.append("")
    lines.extend([
        "## Scientific interpretation limits",
        "",
        "These perturbation results measure sensitivity to modest training "
        "subset changes, randomized forest/model seed changes and a small "
        "number of fixed permutations. They do not establish patient-level "
        "uncertainty or statistically significant between-model differences.",
        "",
        "Large correlated-feature clusters can redistribute attribution "
        "among utilization predictors. A rank change alone is not evidence "
        "of a clinically meaningful or causal association.",
        "",
        "The original SHAP analysis was not refit in these perturbations. "
        "Do not assert SHAP uncertainty or SHAP stability from these results.",
        "",
        "Before publication: internal review of these restricted numbers, "
        "a documented independent patient-level bootstrap if inferential "
        "statements are planned, and institutional disclosure approval.",
        "",
    ])
    return lines


def build_report(populations: dict[str,pd.DataFrame],
                 original_report: dict) -> str:
    lines = [
        "# Protected RF/XGBoost robustness and predictor diagnostics",
        "",
        "**RESTRICTED. Not institutionally disclosure-approved. Do not "
        "upload, email, commit, or copy numerical results into chat.**",
        "",
        "Prespecified Stage A-E results and frozen ETL remain unchanged. "
        "This is a labeled postfreeze exploratory model robustness analysis.",
        "",
        *predictor_diagnostics(populations),
        "",
        *render_stability(compute_seed_stability(populations), original_report),
    ]
    return "\n".join(lines)


def main() -> None:
    try:
        # Keep all inputs at exact approved ignored locations.
        audit.validate_private_path(audit.FILE)
        with audit.FILE.open("r",encoding="utf-8") as handle:
            prior = json.load(handle)
        audit.audit(prior)
        populations = {
            key: m.read_population(
                str(m.PRIVATE_INPUT_ROOT / f"{key}.parquet"), key
            ) for key in m.EXPECTED
        }
        m.validate_populations(populations)
        if DESTINATION.exists() or DESTINATION.is_symlink():
            raise ValueError("Existing stability review must be preserved")
        if (not DESTINATION.parent.is_dir()
                or DESTINATION.parent.is_symlink()
                or stat.S_IMODE(DESTINATION.parent.stat().st_mode) != 0o700):
            raise ValueError("Unsafe restricted output directory")
        text = build_report(populations,prior)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os,"O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with os.fdopen(os.open(DESTINATION,flags,0o600),
                       "w",encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if stat.S_IMODE(DESTINATION.stat().st_mode) != 0o600:
            raise ValueError("Unsafe restricted stability report permissions")
    except Exception:
        print(
            "RF XGBOOST PRIVATE STABILITY REVIEW: BLOCKED "
            "(diagnostics suppressed; preserve all private files).",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("RF XGBOOST PRIVATE STABILITY REVIEW: COMPLETE")
    print("Protected robustness/correlation report created locally.")
    print("No clinical rows, scores, small cells or feature ranks printed.")
    print("Inference and disclosure approval remain outstanding.")


if __name__ == "__main__":
    main()
