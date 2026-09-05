from __future__ import annotations

"""JAMIA-oriented publication figures generated from frozen aggregate artifacts.

This module intentionally leaves the existing Nature-oriented figure pipeline unchanged.
It emphasizes the paper's central distinction between conditional/fixed-cohort fidelity
and independent end-to-end study reproducibility, while using larger typography and
reader-facing precision appropriate for OUP/JAMIA submission review.
"""

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

COLORS = {
    "pcornet": "#0072B2",
    "omop": "#D55E00",
    "fixed": "#0072B2",
    "end": "#D55E00",
    "green": "#009E73",
    "dark": "#222222",
    "mid": "#8A8A8A",
    "light": "#D9D9D9",
    "blue_fill": "#E6F2F8",
    "orange_fill": "#FAECE5",
    "green_fill": "#E5F4EF",
}


def _style() -> None:
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
        "font.size": 9.2,
        "axes.titlesize": 10.2,
        "axes.labelsize": 9.2,
        "xtick.labelsize": 8.7,
        "ytick.labelsize": 8.7,
        "legend.fontsize": 8.8,
        "axes.linewidth": 0.7,
        "lines.linewidth": 1.0,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def _clean(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)


def _panel(ax: plt.Axes, letter: str) -> None:
    ax.text(-0.12, 1.06, letter, transform=ax.transAxes, fontweight="bold", fontsize=12, va="top")


def _box(ax: plt.Axes, xy: tuple[float, float], w: float, h: float, text: str, *, face: str = "white", fs: float = 9.2, bold: bool = False) -> None:
    x, y = xy
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.006,rounding_size=0.01", facecolor=face, edgecolor="#444444", linewidth=0.8))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, fontweight="bold" if bold else "normal", linespacing=1.15)


def _arrow(ax: plt.Axes, a: tuple[float, float], b: tuple[float, float], color: str = "#555555") -> None:
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=10, linewidth=0.9, color=color, shrinkA=0, shrinkB=0))


def figure1_reproducibility_breakpoint(data: dict) -> plt.Figure:
    c, d = data["stage_c"], data["stage_d"]
    fig = plt.figure(figsize=(11, 6.3))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.0, 1.08], left=.045, right=.98, top=.94, bottom=.08, hspace=.20)
    ax = fig.add_subplot(gs[0])
    ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off")
    ax.text(0, 1.01, "Where reproducibility breaks", fontsize=14, fontweight="bold", va="bottom")
    xs, widths = [.02, .23, .44, .65, .82], [.17, .17, .17, .13, .16]
    items = [
        ("Mapped semantics\nExact in locked\nmapped denominators", COLORS["blue_fill"]),
        (f"Source-faithful cohort\nD0: {c['primary']['D0']['pcornet']:,} vs {c['primary']['D0']['omop']:,}\nJaccard {c['primary']['D0']['jaccard']:.3f}", COLORS["orange_fill"]),
        ("Mechanism\nMissing DX_DATE\nsource fallback vs ETL exclusion", COLORS["orange_fill"]),
        ("Harmonize rule\nRequire nonmissing\nDX_DATE in both", COLORS["green_fill"]),
        ("Phenotype rescued\nD0/D1/D3\nJaccard 1.000\nindex dates 100% exact", COLORS["green_fill"]),
    ]
    for i, (x, w, (txt, fc)) in enumerate(zip(xs, widths, items)):
        _box(ax, (x, .37), w, .40, txt, face=fc, fs=9.0, bold=i in {0, 1, 4})
        if i < len(xs) - 1:
            _arrow(ax, (x + w, .57), (xs[i + 1], .57), COLORS["mid"])
    ax.text(.02, .14, "Technical fidelity can be high while cohort reproducibility fails upstream.", fontsize=10.2, fontweight="bold")

    ax = fig.add_subplot(gs[1]); ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off")
    ax.text(0, .98, "The same transformed data answer two different reproducibility questions", fontsize=12, fontweight="bold", va="top")
    _box(ax, (.03, .50), .28, .30, "FIXED PATIENT + INDEX\nCommon observability", face=COLORS["blue_fill"], fs=10, bold=True)
    _box(ax, (.36, .50), .26, .30, f"90-day risk\n{d['fixed']['90_day']['pcornet_risk_percent']:.1f}% = {d['fixed']['90_day']['omop_risk_percent']:.1f}%\n1,132/1,132 first-event dates exact", face=COLORS["green_fill"], fs=10, bold=True)
    _box(ax, (.67, .50), .28, .30, "Representation preserved\nfor the same study anchors", face=COLORS["green_fill"], fs=10, bold=True)
    _arrow(ax, (.31, .65), (.36, .65), COLORS["fixed"]); _arrow(ax, (.62, .65), (.67, .65), COLORS["fixed"])
    _box(ax, (.03, .08), .28, .30, "INDEPENDENT END-TO-END\nStudy built in each CDM", face=COLORS["orange_fill"], fs=10, bold=True)
    _box(ax, (.36, .08), .26, .30, f"90-day risk\n{d['end_to_end']['90_day']['pcornet_risk_percent']:.1f}% vs {d['end_to_end']['90_day']['omop_risk_percent']:.1f}%\nΔ +{d['end_to_end']['90_day']['risk_difference_pp']:.2f} pp; RR {d['end_to_end']['90_day']['rr']:.2f}", face=COLORS["orange_fill"], fs=10, bold=True)
    _box(ax, (.67, .08), .28, .30, f"Population changed\n{d['end_to_end']['90_day']['pcornet_eligible']:,} vs {d['end_to_end']['90_day']['omop_eligible']:,} eligible", face=COLORS["orange_fill"], fs=10, bold=True)
    _arrow(ax, (.31, .23), (.36, .23), COLORS["end"]); _arrow(ax, (.62, .23), (.67, .23), COLORS["end"])
    return fig


def figure2_phenotype_mechanism(data: dict) -> plt.Figure:
    p, h = data["stage_c"]["primary"], data["stage_c"]["harmonized_dxdate"]
    ph, y = ["D0", "D1", "D3"], np.arange(3)[::-1]
    fig = plt.figure(figsize=(10.5, 6.6))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.15, 1], height_ratios=[1, 1], left=.10, right=.98, top=.94, bottom=.10, hspace=.46, wspace=.32)
    ax = fig.add_subplot(gs[:, 0])
    for yi, k in zip(y, ph):
        a, b = p[k]["pcornet"], p[k]["omop"]
        ax.plot([b, a], [yi, yi], color=COLORS["light"], lw=2)
        ax.scatter(a, yi, s=75, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.5, zorder=3)
        ax.scatter(b, yi, s=75, color=COLORS["omop"], zorder=3)
        ax.text(a, yi + .12, f"{a:,}", ha="center", va="bottom", fontsize=9)
        ax.text(b, yi - .12, f"{b:,}", ha="center", va="top", fontsize=9)
    ax.set(yticks=y, yticklabels=ph, xlabel="Patients"); ax.set_title("Source-faithful phenotype size", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "a")
    ax.legend(handles=[plt.Line2D([], [], marker="o", mfc="white", mec=COLORS["pcornet"], ls="None", label="PCORnet"), plt.Line2D([], [], marker="o", color=COLORS["omop"], ls="None", label="OMOP")], loc="upper left", frameon=False, ncol=2)

    ax = fig.add_subplot(gs[0, 1])
    for yi, k in zip(y, ph):
        a, b = p[k]["jaccard"], h[k]["jaccard"]
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2)
        ax.scatter(a, yi, s=60, facecolor="white", edgecolor=COLORS["dark"], linewidth=1.2, zorder=3)
        ax.scatter(b, yi, s=60, color=COLORS["green"], zorder=3)
        ax.text(a, yi - .20, f"{a:.3f}", ha="center", va="top", fontsize=8.8)
        ax.text(b, yi - .20, "1.000", ha="center", va="top", fontsize=8.8, fontweight="bold")
    ax.set(yticks=y, yticklabels=ph, xlabel="Patient Jaccard"); ax.set_xlim(.57, 1.03); ax.set_title("One eligibility rule restores exact membership", loc="left", fontweight="bold", pad=12); _clean(ax); _panel(ax, "b")
    ax.legend(handles=[plt.Line2D([], [], marker="o", mfc="white", mec=COLORS["dark"], ls="None", label="Source-faithful"), plt.Line2D([], [], marker="o", color=COLORS["green"], ls="None", label="Symmetric nonmissing DX_DATE")], loc="lower right", frameon=False)

    ax = fig.add_subplot(gs[1, 1]); ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off"); _panel(ax, "c")
    ax.text(0, 1.02, "Mechanism localized by lineage audit", fontsize=10.2, fontweight="bold", va="bottom")
    _box(ax, (.18, .76), .64, .16, "Selected stroke diagnosis has missing DX_DATE", face=COLORS["orange_fill"], fs=8.8, bold=True)
    _arrow(ax, (.50, .76), (.50, .69))
    _box(ax, (.05, .50), .40, .17, "PCORnet phenotype\nuses encounter-date fallback", face=COLORS["blue_fill"], fs=8.6, bold=True)
    _box(ax, (.55, .50), .40, .17, "Frozen ETL excludes\nthe diagnosis event", face=COLORS["orange_fill"], fs=8.6, bold=True)
    _arrow(ax, (.50, .69), (.25, .67)); _arrow(ax, (.50, .69), (.75, .67))
    ax.text(.75, .42, "No diagnosis lineage for the selected episode", ha="center", va="center", fontsize=8.5, fontweight="bold")
    _arrow(ax, (.75, .50), (.75, .45), COLORS["end"]); _arrow(ax, (.75, .38), (.50, .29), COLORS["green"])
    _box(ax, (.12, .06), .76, .22, "Symmetric nonmissing-DX_DATE eligibility\nD0/D1/D3 Jaccard 1.000; index dates 100% exact", face=COLORS["green_fill"], fs=9.2, bold=True)
    return fig


def figure3_outcome_estimands(data: dict) -> plt.Figure:
    d, labels, y = data["stage_d"], ["30 days", "90 days"], np.array([1, 0])
    fig = plt.figure(figsize=(10.5, 6.3)); gs = fig.add_gridspec(2, 2, left=.10, right=.98, top=.93, bottom=.11, hspace=.42, wspace=.32)
    ax = fig.add_subplot(gs[0, 0])
    for yi, key in zip(y, ["30_day", "90_day"]):
        r = d["fixed"][key]; a, b = r["pcornet_risk_percent"], r["omop_risk_percent"]
        ax.scatter(a, yi, s=75, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.5, zorder=3); ax.scatter(b, yi, s=40, color=COLORS["omop"], zorder=4)
        ax.text(max(a, b) + .4, yi, f"{a:.1f}% = {b:.1f}%", va="center", fontsize=9.4, fontweight="bold")
    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)"); ax.set_title("Fixed patient + index: outcome representation is exact", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "a"); ax.set_xlim(14, 33)
    ax = fig.add_subplot(gs[0, 1])
    for yi, key in zip(y, ["30_day", "90_day"]):
        r = d["end_to_end"][key]; a, b = r["pcornet_risk_percent"], r["omop_risk_percent"]
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2); ax.scatter(a, yi, s=70, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.5); ax.scatter(b, yi, s=60, color=COLORS["omop"])
        ax.text(34.5, yi, f"Δ +{r['risk_difference_pp']:.2f} pp  ·  RR {r['rr']:.2f}", ha="right", va="center", fontsize=8.8, fontweight="bold")
    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)"); ax.set_title("Independent study: risk changes with the population", loc="left", fontweight="bold", pad=10); _clean(ax); _panel(ax, "b"); ax.set_xlim(14, 35)
    ax = fig.add_subplot(gs[1, 0])
    vals = [d["fixed"]["30_day"]["risk_difference_pp"], d["fixed"]["90_day"]["risk_difference_pp"], d["end_to_end"]["30_day"]["risk_difference_pp"], d["end_to_end"]["90_day"]["risk_difference_pp"]]
    labs, yy, m = ["Fixed 30 d", "Fixed 90 d", "End-to-end 30 d", "End-to-end 90 d"], np.arange(4)[::-1], d["equivalence_margins"]["risk_difference_pp"]
    ax.axvspan(-m, m, color=COLORS["green_fill"]); ax.axvline(0, color=COLORS["dark"], lw=.8)
    ax.scatter(vals, yy, s=60, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(vals, yy): ax.text(x + .06, yi, f"{x:+.2f}", va="center", fontsize=9)
    ax.set(yticks=yy, yticklabels=labs, xlabel="OMOP − PCORnet risk (percentage points)"); ax.set_title("Prespecified reproducibility tolerance: ±0.5 pp", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "c"); ax.set_xlim(-.65, 2.25)
    ax = fig.add_subplot(gs[1, 1])
    pairs = [(d["fixed"]["30_day"]["eligible"], d["fixed"]["30_day"]["eligible"]), (d["fixed"]["90_day"]["eligible"], d["fixed"]["90_day"]["eligible"]), (d["end_to_end"]["30_day"]["pcornet_eligible"], d["end_to_end"]["30_day"]["omop_eligible"]), (d["end_to_end"]["90_day"]["pcornet_eligible"], d["end_to_end"]["90_day"]["omop_eligible"])]
    for yi, (a, b) in zip(yy, pairs):
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2); ax.scatter(a, yi, s=65, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.5); ax.scatter(b, yi, s=50, color=COLORS["omop"])
    ax.set(yticks=yy, yticklabels=labs, xlabel="Eligible patients"); ax.set_title("The divergence enters through cohort selection", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "d")
    return fig


def figure4_model_reproducibility(data: dict) -> plt.Figure:
    e = data["stage_e"]
    features = ["Age", "Female", "Index length of stay", "Prior acute-care encounters", "Prior all encounters", "Prior ischemic stroke"]
    y, models, mlabs, ym = np.arange(6)[::-1], list(e["models"]), ["Logistic", "Ridge logistic", "Gradient boosting"], np.arange(3)[::-1]
    fig = plt.figure(figsize=(10.5, 6.3)); gs = fig.add_gridspec(2, 2, left=.18, right=.98, top=.93, bottom=.11, hspace=.42, wspace=.32)
    ax = fig.add_subplot(gs[:, 0]); fs = [abs(e["fixed_feature_smd"][x]) for x in features]; es = [abs(e["end_to_end_feature_smd"][x]) for x in features]
    ax.axvline(.10, color=COLORS["mid"], ls="--", lw=.9)
    for yi, a, b in zip(y, fs, es):
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2); ax.scatter(a, yi, s=65, facecolor="white", edgecolor=COLORS["fixed"], linewidth=1.4, zorder=3); ax.scatter(b, yi, s=60, color=COLORS["end"], zorder=3)
        if b >= .10: ax.text(b + .006, yi, f"{b:.2f}", va="center", fontsize=8.8, fontweight="bold")
    ax.set(yticks=y, yticklabels=features, xlabel="Absolute standardized mean difference"); ax.set_title("Population shift appears only end-to-end", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "a"); ax.set_xlim(-.005, .18)
    ax.legend(handles=[plt.Line2D([], [], marker="o", mfc="white", mec=COLORS["fixed"], ls="None", label="Fixed cohort"), plt.Line2D([], [], marker="o", color=COLORS["end"], ls="None", label="End-to-end")], loc="lower right", frameon=False)
    ax = fig.add_subplot(gs[0, 1]); fa = [e["models"][m]["fixed_auroc_difference"] for m in models]; ea = [e["models"][m]["end_auroc_difference"] for m in models]
    ax.axvline(0, color=COLORS["dark"], lw=.8)
    for yi, a, b in zip(ym, fa, ea):
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2); ax.scatter(a, yi, s=60, facecolor="white", edgecolor=COLORS["fixed"], linewidth=1.4); ax.scatter(b, yi, s=55, color=COLORS["end"]); ax.text(b - .001, yi + .12, f"{b:.2f}", ha="right", fontsize=8.8, fontweight="bold")
    ax.set(yticks=ym, yticklabels=mlabs, xlabel="AUROC difference (OMOP − PCORnet)"); ax.set_title("Discrimination: stable fixed, shifted end-to-end", loc="left", fontweight="bold"); _clean(ax); _panel(ax, "b"); ax.set_xlim(-.052, .004)
    ax = fig.add_subplot(gs[1, 1]); mad = [e["models"][m]["fixed_probability_mad"] for m in models]; bd = [e["models"][m]["end_omop_brier"] - e["models"][m]["end_pcornet_brier"] for m in models]
    ax.set(xlim=(0, 1), ylim=(-.5, 2.5)); ax.axis("off"); _panel(ax, "c")
    ax.text(0, 2.65, "Patient-level agreement vs end-to-end prediction error", fontsize=10.2, fontweight="bold", va="top")
    ax.text(.48, 2.28, "Fixed prediction MAD", ha="right", fontsize=9, fontweight="bold"); ax.text(.96, 2.28, "End-to-end Brier Δ", ha="right", fontsize=9, fontweight="bold")
    for yi, label, mv, bv in zip(ym, mlabs, mad, bd):
        ax.text(0, yi, label, va="center", fontsize=9.1); ax.text(.48, yi, "<0.001" if mv < .001 else f"{mv:.3f}", ha="right", va="center", fontsize=9.4, fontweight="bold" if mv < .001 else "normal", color=COLORS["fixed"]); ax.text(.96, yi, f"{bv:+.2f}", ha="right", va="center", fontsize=9.4, color=COLORS["end"]); ax.plot([.52, .72], [yi, yi], color=COLORS["light"], lw=2)
    ax.text(0, -.35, "Fixed logistic predictions are nearly identical; end-to-end differences reflect different empirical populations.", fontsize=8.8)
    return fig


def extended1_semantic_fidelity(data: dict) -> plt.Figure:
    b = data["stage_b"]; fig, axs = plt.subplots(1, 3, figsize=(10.5, 4.6), gridspec_kw={"width_ratios": [1.2, 1, 1.1]}); fig.subplots_adjust(left=.12, right=.98, top=.88, bottom=.18, wspace=.45)
    ax = axs[0]; names, vals = list(b["mapped_exact_counts"]), list(b["mapped_exact_counts"].values()); y = np.arange(len(names))[::-1]; ax.scatter(vals, y, s=50, color=COLORS["pcornet"]); ax.set_xscale("log"); ax.set_yticks(y, labels=names); ax.set_xlabel("Exact mapped rows (log scale)"); ax.set_title("Mapped semantic fidelity", fontweight="bold"); _clean(ax); _panel(ax, "a")
    ax = axs[1]; pct = 100 * b["numeric"]["direct_exact"] / b["numeric"]["comparable"]; ax.barh([1, 0], [pct, 100], height=.55); ax.set_xlim(0, 104); ax.set_yticks([1, 0], labels=["Directly exact\namong comparable", "Explained among\ninitial differences"]); ax.set_xlabel("Rows (%)"); ax.set_title("Numeric reconciliation", fontweight="bold"); _clean(ax); ax.text(pct + 1, 1, f"{pct:.1f}%", va="center"); ax.text(101, 0, "100%", va="center"); _panel(ax, "b")
    ax = axs[2]; names = ["Condition concept-0", "Procedure unresolved", "Drug concept-0", "Meas./obs. unresolved"]; vals = list(b["coverage_limitations"].values()); yy = np.arange(4)[::-1]; ax.scatter(vals, yy, s=50, color=COLORS["omop"]); ax.set_xscale("log"); ax.set_yticks(yy, labels=names); ax.set_xlabel("Rows/routes (log scale)"); ax.set_title("Coverage limitations kept separate", fontweight="bold"); _clean(ax); _panel(ax, "c")
    return fig


def extended2_additional_reproducibility(data: dict) -> plt.Figure:
    e, d = data["stage_e"], data["stage_d"]["recurrent"]; fig, axs = plt.subplots(1, 3, figsize=(10.5, 4.4)); fig.subplots_adjust(left=.13, right=.98, top=.86, bottom=.18, wspace=.50)
    ax = axs[0]; feats = ["Age", "Female", "Index length of stay", "Prior acute-care encounters", "Prior all encounters", "Prior ischemic stroke"]; vals = [e["fixed_association_or_ratio_omop_over_source"][f] for f in feats]; y = np.arange(6)[::-1]; ax.axvline(1, color=COLORS["dark"], lw=.8); ax.scatter(vals, y, s=50, color=COLORS["pcornet"]); ax.set_yticks(y, labels=feats); ax.set_xlim(.9991, 1.0007); ax.set_xlabel("OMOP / PCORnet odds-ratio ratio"); ax.set_title("Fixed-cohort associations", fontweight="bold"); _clean(ax); _panel(ax, "a")
    ax = axs[1]; models, labs = list(e["models"]), ["Logistic", "Ridge logistic", "Gradient boosting"]; vals = [e["models"][m]["fixed_probability_pearson"] for m in models]; yy = np.arange(3)[::-1]; ax.scatter(vals, yy, s=50, color=COLORS["pcornet"]); ax.set_yticks(yy, labels=labs); ax.set_xlim(.94, 1.002); ax.set_xlabel("Pearson correlation"); ax.set_title("Fixed prediction agreement", fontweight="bold"); _clean(ax); _panel(ax, "b")
    for x, yi in zip(vals, yy): ax.text(x - .001, yi + .12, ">0.999" if x > .999 else f"{x:.3f}", ha="right", fontsize=8.8)
    ax = axs[2]; labels = ["Primary recurrent\nstroke-code endpoint", "Post-outcome\nPDX=P sensitivity"]; yy = np.array([1, 0]); pc = [d["primary_pcornet_events"], d["pdx_primary_sensitivity_pcornet_events"]]; om = [d["primary_omop_events"], d["pdx_primary_sensitivity_omop_events"]]
    for yi, a, b in zip(yy, pc, om): ax.scatter(a, yi + .05, s=55, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.4); ax.scatter(b, yi - .05, s=50, color=COLORS["omop"]); ax.text(a + 2, yi + .10, str(a), fontsize=8.8); ax.text(b + 2, yi - .16, str(b), fontsize=8.8)
    ax.set_yticks(yy, labels=labels); ax.set_xlim(150, 280); ax.set_xlabel("Patients with recurrent event"); ax.set_title("Recurrent-stroke sensitivity", fontweight="bold"); _clean(ax); _panel(ax, "c")
    return fig


def extended3_calibration(data: dict) -> plt.Figure:
    e = data["stage_e"]["calibration"]; models, labs, y = list(e), ["Logistic", "Ridge logistic", "Gradient boosting"], np.arange(3)[::-1]; fig, axs = plt.subplots(1, 2, figsize=(9.5, 4.6)); fig.subplots_adjust(left=.14, right=.98, top=.88, bottom=.20, wspace=.40)
    for j, metric in enumerate(["slope", "intercept"]):
        ax, ref = axs[j], 1 if metric == "slope" else 0; ax.axvline(ref, color=COLORS["dark"], ls="--", lw=.8)
        for yi, m in zip(y, models):
            f, z = e[m]["fixed"], e[m]["end_to_end"]
            for rep, offset in [("pcornet", .10), ("omop", -.10)]:
                fv, ev = f[f"{rep}_{metric}"], z[f"{rep}_{metric}"]; ax.plot([fv, ev], [yi + offset, yi + offset], color=COLORS["light"], lw=2); ax.scatter(fv, yi + offset, s=50, facecolor="white" if rep == "pcornet" else COLORS["fixed"], edgecolor=COLORS["fixed"], linewidth=1.2, zorder=3); ax.scatter(ev, yi + offset, s=50, facecolor="white" if rep == "pcornet" else COLORS["end"], edgecolor=COLORS["end"], linewidth=1.2, zorder=3)
        ax.set_yticks(y, labels=labs); ax.set_xlabel(f"Calibration {metric}"); ax.set_title(f"Calibration {metric}", fontweight="bold"); _clean(ax); _panel(ax, chr(ord("a") + j))
    return fig


def _save_all(fig: plt.Figure, stem: Path) -> None:
    fig.savefig(stem.with_suffix(".png"), dpi=300, bbox_inches="tight")
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(stem.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)


def build_all(data: dict, outdir: Path) -> None:
    builders = {
        "Figure1_reproducibility_breakpoint": figure1_reproducibility_breakpoint,
        "Figure2_phenotype_mechanism": figure2_phenotype_mechanism,
        "Figure3_outcome_estimands": figure3_outcome_estimands,
        "Figure4_model_reproducibility": figure4_model_reproducibility,
        "ExtendedDataFigure1_semantic_fidelity": extended1_semantic_fidelity,
        "ExtendedDataFigure2_additional_reproducibility": extended2_additional_reproducibility,
        "ExtendedDataFigure3_calibration": extended3_calibration,
    }
    outdir.mkdir(parents=True, exist_ok=True)
    for name, builder in builders.items():
        _save_all(builder(data), outdir / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="study_definitions/artifacts/publication_figure_data_v1.json")
    parser.add_argument("--outdir", default="figures/jamia")
    args = parser.parse_args()
    _style()
    data = json.loads(Path(args.data).read_text())
    build_all(data, Path(args.outdir))


if __name__ == "__main__":
    main()
