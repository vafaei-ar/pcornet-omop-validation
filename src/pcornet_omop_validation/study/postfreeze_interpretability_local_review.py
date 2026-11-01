"""Create a restricted, detailed local scientific review of RF/XGBoost attribution.

Reads the already-audited institutional aggregate only. Does not connect to SQL,
read the four protected Parquet cohorts, fit models, or disclose result numbers
through stdout. All detailed predictor names/ranks/scores stay in a 0600 ignored
local Markdown file awaiting scientific and institutional disclosure review.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from pcornet_omop_validation.study import (
    postfreeze_model_interpretability as m,
    postfreeze_model_interpretability_audit as a,
)

LOCAL_REVIEW = m.PRIVATE_RESULTS_ROOT / "private_scientific_review.md"
IMPORTANT_FIELDS = (
    ("permutation_auroc_decrease_mean", "Permutation AUROC decrease"),
    ("mean_absolute_shap_probability", "Mean absolute SHAP probability"),
)


def _format(value: float) -> str:
    return f"{float(value):+.5f}"


def _signed(value: float, tolerance: float = 1e-10) -> int:
    return 1 if value > tolerance else (-1 if value < -tolerance else 0)


def rank_agreement(left: list[str], right: list[str]) -> int:
    """How many features have the identical rank under two explanations."""
    return sum(left.index(f) == right.index(f) for f in left)


def comparison_table(contrast: dict, left: dict, right: dict,
                     features: list[str]) -> list[str]:
    lines = [
        "| Feature | Permutation left | Permutation right | Delta (right-left) | "
        "Permutation rank L to R | SHAP left | SHAP right | SHAP delta | SHAP rank L to R |",
        "| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | :---: |",
    ]
    for feature in features:
        l = left["features"][feature]
        r = right["features"][feature]
        d = contrast["feature_differences"][feature]
        lines.append(
            "| `" + feature + "` | "
            + " | ".join([
                _format(l["permutation_auroc_decrease_mean"]),
                _format(r["permutation_auroc_decrease_mean"]),
                _format(d["permutation_auroc_decrease_difference"]),
                f'{d["permutation_rank_left"]} to {d["permutation_rank_right"]}',
                _format(l["mean_absolute_shap_probability"]),
                _format(r["mean_absolute_shap_probability"]),
                _format(d["mean_absolute_shap_probability_difference"]),
                f'{d["shap_rank_left"]} to {d["shap_rank_right"]}',
            ]) + " |"
        )
    return lines


def build_review(report: dict) -> str:
    """Only call on the private host, after a.audit(report) succeeds."""
    lines = [
        "# RF/XGBoost feature-attribution review",
        "",
        "**RESTRICTED, NOT DISCLOSURE-APPROVED. Do not upload, email, commit, "
        "copy into chat, or use these numbers in a manuscript yet.**",
        "",
        "This report describes a post-freeze exploratory extension. It does "
        "not change the frozen ETL or Stage A-E results. Feature attributions "
        "are properties of fitted models, input representations, test "
        "populations, and the chosen explanation method. They are not "
        "causal clinical risk-factor estimates.",
        "",
        "## Study design and scope",
        "",
        "- Outcome: locked 90-day acute-care reutilization.",
        "- Algorithms: Random Forest and XGBoost; fixed patient-hash 70/30 split.",
        "- Six locked predictors, with a prespecified five-predictor sensitivity "
        "excluding diagnosis-date-sensitive prior stroke.",
        "- Primary: full held-out, 20-repeat AUROC permutation importance.",
        "- Secondary: interventional, probability-scale mean absolute SHAP "
        "on at most 500 held-out patients, using a shared source training background.",
        "- The native end-to-end contrast compares different eligible populations. "
        "Do not describe it as the causal effect of the OMOP standard.",
        "- Matched common-test comparisons control evaluation membership and labels "
        "but do not eliminate model-training population or representation differences.",
        "",
        "## Held-out predictive performance",
        "",
        "The numerical performance values below are for restricted local review. "
        "Differences in AUROC or Brier score are not paired confidence intervals.",
        "",
        "| Feature set | Model | Population | Native AUROC | Native AUPRC | "
        "Native Brier | Matched AUROC | Matched Brier |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sens in a.SENSITIVITIES:
        for algo in a.MODELS:
            for group in m.EXPECTED:
                x = report["models"][sens][algo][group]
                n = x["native_test_performance"]
                c = x["common_shared_test_performance"]
                lines.append(
                    f'| {sens} | {algo} | {group} | '
                    f'{n["auroc"]:.4f} | {n["auprc"]:.4f} | {n["brier"]:.4f} | '
                    f'{c["auroc"]:.4f} | {c["brier"]:.4f} |'
                )
    lines.extend([
        "",
        "## Attribution contrasts: full private numerical detail",
        "",
        "Delta convention: right minus left. Negative permutation importance "
        "is possible and should not be deleted or converted to zero. "
        "The permutation SD over 20 shuffles is **not** a patient-bootstrap "
        "uncertainty interval or hypothesis test.",
        "",
    ])
    for sens, features in a.SENSITIVITIES.items():
        lines.extend([f"### {sens}", ""])
        for label, (left_name, right_name, scope) in a.PAIRS.items():
            lines.extend([
                f"#### {label}",
                "",
                f"Left: `{left_name}`. Right: `{right_name}`. "
                f"Evaluation: `{scope}`.",
                "",
            ])
            for algo in a.MODELS:
                data = report["models"][sens][algo]
                l = data[left_name][scope + "_importance"]
                r = data[right_name][scope + "_importance"]
                contrast = report["feature_importance_comparisons"][sens][algo][label]
                lines.extend([
                    f"**{algo}**",
                    "",
                    f'Permutation rank-1 change: `{l["permutation_rank"][0]}` '
                    f'to `{r["permutation_rank"][0]}`. '
                    f'SHAP rank-1 change: `{l["shap_rank"][0]}` '
                    f'to `{r["shap_rank"][0]}`.',
                    "",
                    f'Within-left permutation/SHAP identical rank positions: '
                    f'{rank_agreement(l["permutation_rank"], l["shap_rank"])} '
                    f'of {len(features)}. Within-right: '
                    f'{rank_agreement(r["permutation_rank"], r["shap_rank"])} '
                    f'of {len(features)}.',
                    "",
                    *comparison_table(contrast, l, r, features),
                    "",
                ])
            for field, name in IMPORTANT_FIELDS:
                sub = (
                    "permutation_auroc_decrease_difference"
                    if field == "permutation_auroc_decrease_mean"
                    else "mean_absolute_shap_probability_difference"
                )
                deltas = [
                    report["feature_importance_comparisons"][sens][algo][label]
                    ["feature_differences"]
                    for algo in a.MODELS
                ]
                agree = sum(
                    _signed(deltas[0][feature][sub])
                    == _signed(deltas[1][feature][sub])
                    for feature in features
                )
                lines.append(
                    f"RF/XGBoost delta-direction agreement for {name}: "
                    f"{agree} of {len(features)} features "
                    "(including near-zero as its own category)."
                )
            lines.extend(["", ""])
    lines.extend([
        "## Interpretation decisions requiring internal review",
        "",
        "1. Does the native end-to-end rank/magnitude contrast persist on "
        "the matched-shared test set? Compare selection and frozen representation separately.",
        "2. Does the full-PCORnet versus fallback-OMOP comparison attenuate "
        "differences when all 6,508 source-eligible patients are preserved?",
        "3. Are apparently large changes limited to diagnosis-date-sensitive "
        "prior stroke, and do they remain after its exclusion?",
        "4. Does direction agree across RF and XGBoost, permutation importance "
        "and SHAP? Account for correlated prior-utilization features.",
        "5. Were test-case resampling uncertainty, training-seed stability "
        "and feature-correlation diagnostics actually evaluated? "
        "**Not in this saved aggregate.** Do not call the results robust "
        "or statistically significant based on permutation-repeat SD alone.",
        "6. Do not compare SHAP magnitudes as invariant clinical effects. "
        "They depend on the model, reference distribution, and imputation.",
        "",
        "## Publication decision",
        "",
        "**Open.** Independent model/report validation passed, but scientific "
        "effect size, stability, uncertainty, interpretability, and "
        "institutional disclosure approval remain outstanding. If rank "
        "differences are fragile, report them descriptively as a supplementary "
        "analysis rather than making a strong main-text claim.",
        "",
    ])
    return "\n".join(lines)


def _validate_destination(path: Path) -> None:
    if path != LOCAL_REVIEW:
        raise a.AuditBlocked("unexpected private review path")
    if path.exists() or path.is_symlink():
        raise a.AuditBlocked("existing private review must not be overwritten")
    if (not path.parent.is_dir() or path.parent.is_symlink()
            or stat.S_IMODE(path.parent.stat().st_mode) != 0o700):
        raise a.AuditBlocked("unsafe local review directory")


def write_review(report: dict, path: Path | None = None) -> None:
    if path is None:
        path = LOCAL_REVIEW
    _validate_destination(path)
    text = build_review(report)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise a.AuditBlocked("private review file permissions")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local private RF/XGBoost scientific review")
    parser.add_argument("--input", default=str(a.FILE))
    args = parser.parse_args()
    try:
        candidate = Path(os.path.abspath(args.input))
        a.validate_private_path(candidate)
        with candidate.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
        a.audit(report)
        write_review(report)
    except Exception:
        # Never show private ranks, metrics, file contents or provider details.
        print("RF XGBOOST PRIVATE SCIENTIFIC REVIEW: BLOCKED", file=sys.stderr)
        raise SystemExit(1) from None
    print("RF XGBOOST PRIVATE SCIENTIFIC REVIEW: WRITTEN")
    print("Report stored in ignored results directory; owner-only access.")
    print("No private scores, patient data or feature rankings printed.")
    print("Stability and institutional disclosure approval remain open.")


if __name__ == "__main__":
    main()
