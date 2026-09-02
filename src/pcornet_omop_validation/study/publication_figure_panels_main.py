from __future__ import annotations

"""Main manuscript figures: validation framework and Stages C-E results."""

import matplotlib.pyplot as plt
import numpy as np

from .publication_figure_style import (
    COLORS, DOUBLE_COLUMN_MM, add_box, arrow, clean_axis, direct_value, mm, panel_label,
)


def figure1_validation_framework(data: dict) -> plt.Figure:
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(108)))
    ax = fig.add_axes([0.02, 0.04, 0.96, 0.92]); ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off")
    add_box(ax, (0.04, 0.82), 0.20, 0.105, "PCORnet source", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.40, 0.82), 0.20, 0.105, "Audited, frozen ETL", facecolor=COLORS["light_gray"], weight="bold")
    add_box(ax, (0.76, 0.82), 0.20, 0.105, "OMOP target", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.24, 0.872), (0.40, 0.872)); arrow(ax, (0.60, 0.872), (0.76, 0.872))

    ax.text(0.02, 0.73, "Validation layers", fontsize=7, fontweight="bold", va="center")
    labels = [("A", "Structure &\neligibility"), ("B", "Mapped clinical\nsemantics"),
              ("C", "Phenotype\nreproducibility"), ("D", "Outcome\nreproducibility"),
              ("E", "Statistics &\nprediction")]
    xs = np.linspace(0.04, 0.78, len(labels)); box_w = 0.16
    for i, ((letter, label), x) in enumerate(zip(labels, xs)):
        face = COLORS["light_green"] if letter in {"C", "D", "E"} else "white"
        add_box(ax, (float(x), 0.61), box_w, 0.10, f"{letter}  {label}", facecolor=face, fontsize=5.7, weight="bold")
        if i < len(labels) - 1:
            arrow(ax, (float(x) + box_w, 0.66), (float(xs[i + 1]), 0.66), COLORS["mid_gray"])

    ax.text(0.02, 0.515, "Two complementary estimands", fontsize=7, fontweight="bold", va="center")
    add_box(ax, (0.04, 0.36), 0.28, 0.11, "Fixed patient + fixed index\n+ common observability", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.36, 0.36), 0.25, 0.11, "Isolate representation\nand feature construction")
    add_box(ax, (0.65, 0.36), 0.31, 0.11, "Exact acute-care outcomes;\nnear-identical logistic analyses", facecolor=COLORS["light_green"], weight="bold")
    arrow(ax, (0.32, 0.415), (0.36, 0.415), COLORS["fixed"]); arrow(ax, (0.61, 0.415), (0.65, 0.415), COLORS["fixed"])
    add_box(ax, (0.04, 0.17), 0.28, 0.11, "Independent end-to-end\nstudy in each CDM", facecolor=COLORS["light_orange"], weight="bold")
    add_box(ax, (0.36, 0.17), 0.25, 0.11, "Includes cohort-selection\nand representation effects")
    add_box(ax, (0.65, 0.17), 0.31, 0.11, "Different cohort composition,\nrisks and model performance", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.32, 0.225), (0.36, 0.225), COLORS["end"]); arrow(ax, (0.61, 0.225), (0.65, 0.225), COLORS["end"])
    ax.text(0.50, 0.055, "Key mechanism: nonmissing DX_DATE required by the frozen ETL -> source episodes can be excluded -> cohort selection changes downstream estimates", fontsize=5.5, ha="center", va="center")
    return fig


def figure2_phenotype_reproducibility(data: dict) -> plt.Figure:
    primary = data["stage_c"]["primary"]; harm = data["stage_c"]["harmonized_dxdate"]
    phenotypes = ["D0", "D1", "D3"]; y = np.arange(3)[::-1]
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(142)))
    gs = fig.add_gridspec(2, 2, left=0.08, right=0.97, top=0.95, bottom=0.08, hspace=0.45, wspace=0.34)

    ax = fig.add_subplot(gs[0, 0]); pc = [primary[p]["pcornet"] for p in phenotypes]; om = [primary[p]["omop"] for p in phenotypes]
    for yi, a, b in zip(y, pc, om):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"]); ax.scatter(a, yi, s=24, color=COLORS["pcornet"], zorder=3); ax.scatter(b, yi, s=24, color=COLORS["omop"], zorder=3)
        direct_value(ax, a, yi + 0.11, f"{a:,}", COLORS["pcornet"], -2); direct_value(ax, b, yi - 0.11, f"{b:,}", COLORS["omop"], 2)
    ax.set(yticks=y, yticklabels=phenotypes, xlim=(0, 10500), xlabel="Patients"); ax.set_title("Source-faithful phenotype size", loc="left"); clean_axis(ax, "x")
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet"); ax.scatter([], [], color=COLORS["omop"], label="OMOP"); ax.legend(loc="upper left", bbox_to_anchor=(0.02, 0.90), ncols=2, handletextpad=0.4, columnspacing=0.9); panel_label(ax, "a")

    ax = fig.add_subplot(gs[0, 1]); jp = [primary[p]["jaccard"] for p in phenotypes]; jh = [harm[p]["jaccard"] for p in phenotypes]
    for yi, a, b in zip(y, jp, jh):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"]); ax.scatter(a, yi + 0.035, s=24, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3); ax.scatter(b, yi - 0.035, s=24, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlim=(0.55, 1.02), xlabel="Patient Jaccard"); ax.set_xticks([0.6, 0.7, 0.8, 0.9, 1.0]); ax.set_title("Eligibility harmonization removes discordance", loc="left"); clean_axis(ax, "x")
    ax.scatter([], [], s=22, facecolor="white", edgecolor=COLORS["primary"], label="Source-faithful"); ax.scatter([], [], s=22, color=COLORS["dark"], label="Harmonized DX_DATE"); ax.legend(loc="center left", bbox_to_anchor=(0.02, 0.73), handletextpad=0.4); panel_label(ax, "b")

    ax = fig.add_subplot(gs[1, 0]); ip = [primary[p]["exact_index_percent"] for p in phenotypes]; ih = [harm[p]["exact_index_percent"] for p in phenotypes]
    for yi, a, b in zip(y, ip, ih):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"]); ax.scatter(a, yi + 0.035, s=24, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3); ax.scatter(b, yi - 0.035, s=24, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlim=(96.5, 100.25), xlabel="Exact index-date agreement among shared patients (%)"); ax.set_xticks([97, 98, 99, 100]); ax.set_title("Index-date agreement", loc="left"); clean_axis(ax, "x"); panel_label(ax, "c")

    ax = fig.add_subplot(gs[1, 1]); ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off"); panel_label(ax, "d", -0.08, 1.04); ax.text(0, 1, "Mechanism localized by lineage audit", fontsize=7, va="top")
    steps = [
        (0.00, 0.63, 0.25, "Selected stroke diagnosis\nhas null DX_DATE", COLORS["light_orange"], 5.0),
        (0.34, 0.63, 0.32, "Source uses encounter-date\nfallback; frozen ETL\nexcludes diagnosis", "white", 5.0),
        (0.74, 0.63, 0.24, "No diagnosis lineage\nfor selected episode", COLORS["light_orange"], 5.0),
    ]
    for x, yy, w, text, fc, fs in steps:
        add_box(ax, (x, yy), w, 0.20, text, facecolor=fc, fontsize=fs, weight="bold" if fc != "white" else "normal")
    arrow(ax, (0.25, 0.73), (0.34, 0.73), COLORS["mid_gray"]); arrow(ax, (0.66, 0.73), (0.74, 0.73), COLORS["mid_gray"])
    add_box(ax, (0.12, 0.20), 0.76, 0.26, "Symmetric nonmissing-DX_DATE eligibility\nD0/D1/D3: Jaccard = 1.000\nIndex dates: 100% exact", facecolor=COLORS["light_green"], fontsize=5.2, weight="bold"); arrow(ax, (0.50, 0.63), (0.50, 0.47), COLORS["harmonized"])
    return fig


def figure3_outcome_reproducibility(data: dict) -> plt.Figure:
    d = data["stage_d"]; labels = ["Fixed 30 d", "Fixed 90 d", "End-to-end 30 d", "End-to-end 90 d"]; y = np.arange(4)[::-1]
    f30, f90 = d["fixed"]["30_day"], d["fixed"]["90_day"]; e30, e90 = d["end_to_end"]["30_day"], d["end_to_end"]["90_day"]; rows = [f30, f90, e30, e90]
    pc_risk = [r["pcornet_risk_percent"] for r in rows]; om_risk = [r["omop_risk_percent"] for r in rows]; diff = [r["risk_difference_pp"] for r in rows]; rr = [r["rr"] for r in rows]
    pc_n = [f30["eligible"], f90["eligible"], e30["pcornet_eligible"], e90["pcornet_eligible"]]; om_n = [f30["eligible"], f90["eligible"], e30["omop_eligible"], e90["omop_eligible"]]
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(128))); gs = fig.add_gridspec(2, 2, left=0.105, right=0.975, top=0.95, bottom=0.10, hspace=0.43, wspace=0.38)

    ax = fig.add_subplot(gs[0, 0])
    for yi, a, b in zip(y, pc_risk, om_risk):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"]); ax.scatter(a, yi + 0.035, s=24, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3); ax.scatter(b, yi - 0.035, s=20, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlim=(0, 33), xlabel="Acute-care risk (%)"); ax.set_title("Observed risks", loc="left"); clean_axis(ax, "x"); ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet"); ax.scatter([], [], color=COLORS["omop"], label="OMOP"); ax.legend(loc="upper left", ncols=2, handletextpad=0.4, columnspacing=0.8); panel_label(ax, "a", -0.18)

    ax = fig.add_subplot(gs[0, 1]); m = float(d["equivalence_margins"]["risk_difference_pp"]); ax.axvspan(-m, m, color=COLORS["light_green"], zorder=0); ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(diff, y, s=25, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(diff, y): direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlim=(-0.65, 2.30), xlabel="OMOP - PCORnet risk (percentage points)"); ax.set_title("Prespecified absolute equivalence", loc="left"); clean_axis(ax, "x"); panel_label(ax, "b", -0.18)

    ax = fig.add_subplot(gs[1, 0]); lo, hi = d["equivalence_margins"]["rr_lower"], d["equivalence_margins"]["rr_upper"]; ax.axvspan(lo, hi, color=COLORS["light_green"], zorder=0); ax.axvline(1, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(rr, y, s=25, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(rr, y): direct_value(ax, x, yi, f"{x:.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlim=(0.93, 1.095), xlabel="OMOP / PCORnet risk ratio"); ax.set_title("Prespecified relative equivalence", loc="left"); clean_axis(ax, "x"); panel_label(ax, "c", -0.18)

    ax = fig.add_subplot(gs[1, 1])
    for yi, a, b in zip(y, pc_n, om_n):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"]); ax.scatter(a, yi + 0.035, s=24, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3); ax.scatter(b, yi - 0.035, s=20, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlim=(0, 8000), xlabel="Eligible patients"); ax.set_title("Population entering each estimand", loc="left"); clean_axis(ax, "x"); panel_label(ax, "d", -0.18)
    return fig


def figure4_model_reproducibility(data: dict) -> plt.Figure:
    e = data["stage_e"]; features = ["Age", "Female", "Index length of stay", "Prior acute-care encounters", "Prior all encounters", "Prior ischemic stroke"]; yf = np.arange(6)[::-1]
    fs = [abs(e["fixed_feature_smd"][f]) for f in features]; es = [abs(e["end_to_end_feature_smd"][f]) for f in features]
    models = list(e["models"]); short = ["Logistic", "Ridge logistic", "Gradient boosting"]; ym = np.arange(3)[::-1]
    fa = [e["models"][m]["fixed_auroc_difference"] for m in models]; ea = [e["models"][m]["end_auroc_difference"] for m in models]; mad = [e["models"][m]["fixed_probability_mad"] for m in models]; bd = [e["models"][m]["end_omop_brier"] - e["models"][m]["end_pcornet_brier"] for m in models]
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(137))); gs = fig.add_gridspec(2, 2, left=0.14, right=0.975, top=0.95, bottom=0.10, hspace=0.45, wspace=0.40)

    ax = fig.add_subplot(gs[0, 0]); ax.axvline(0.10, color=COLORS["mid_gray"], linewidth=0.6, linestyle="--")
    for yi, a, b in zip(yf, fs, es): ax.plot([a, b], [yi, yi], color=COLORS["grid"]); ax.scatter(a, yi, s=22, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3); ax.scatter(b, yi, s=22, color=COLORS["end"], zorder=3)
    ax.set(yticks=yf, yticklabels=features, xlim=(-0.005, 0.18), xlabel="Absolute standardized mean difference"); ax.set_title("Feature distributions", loc="left"); clean_axis(ax, "x"); ax.scatter([], [], s=20, facecolor="white", edgecolor=COLORS["fixed"], label="Fixed cohort"); ax.scatter([], [], s=20, color=COLORS["end"], label="End-to-end"); ax.legend(loc="lower right", handletextpad=0.4); panel_label(ax, "a", -0.28)

    ax = fig.add_subplot(gs[0, 1]); ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    for yi, a, b in zip(ym, fa, ea): ax.plot([a, b], [yi, yi], color=COLORS["grid"]); ax.scatter(a, yi, s=22, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3); ax.scatter(b, yi, s=22, color=COLORS["end"], zorder=3)
    ax.set(yticks=ym, yticklabels=short, xlim=(-0.050, 0.004), xlabel="AUROC difference (OMOP - PCORnet)"); ax.set_title("Model discrimination", loc="left"); clean_axis(ax, "x"); panel_label(ax, "b", -0.24)

    ax = fig.add_subplot(gs[1, 0]); ax.scatter(mad, ym, s=24, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(mad, ym): direct_value(ax, x, yi, f"{x:.5f}" if x < 0.001 else f"{x:.3f}", COLORS["dark"], 5)
    ax.set(yticks=ym, yticklabels=short, xlim=(-0.002, 0.043), xlabel="Mean absolute prediction difference"); ax.set_title("Fixed-cohort individual predictions", loc="left"); clean_axis(ax, "x"); panel_label(ax, "c", -0.28)

    ax = fig.add_subplot(gs[1, 1]); ax.axvline(0, color=COLORS["dark"], linewidth=0.6); ax.scatter(bd, ym, s=24, color=COLORS["end"], zorder=3)
    for x, yi in zip(bd, ym): direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], 5)
    ax.set(yticks=ym, yticklabels=short, xlim=(-0.001, 0.021), xlabel="Brier difference (OMOP - PCORnet)"); ax.set_title("End-to-end prediction error", loc="left"); clean_axis(ax, "x"); panel_label(ax, "d", -0.24)
    return fig


MAIN_FIGURE_BUILDERS = {
    "Figure1_validation_framework": figure1_validation_framework,
    "Figure2_phenotype_reproducibility": figure2_phenotype_reproducibility,
    "Figure3_outcome_reproducibility": figure3_outcome_reproducibility,
    "Figure4_model_reproducibility": figure4_model_reproducibility,
}
