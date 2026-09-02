from __future__ import annotations

"""Main manuscript figures: validation framework and Stages C-E results."""

import matplotlib.pyplot as plt
import numpy as np

from .publication_figure_style import (
    COLORS,
    DOUBLE_COLUMN_MM,
    add_box,
    arrow,
    clean_axis,
    direct_value,
    mm,
    padded_limits,
    panel_label,
)


def figure1_validation_framework(data: dict) -> plt.Figure:
    del data
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(92)))
    ax = fig.add_axes([0.025, 0.045, 0.95, 0.92])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")

    add_box(ax, (0.04, 0.82), 0.20, 0.11, "PCORnet source", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.40, 0.82), 0.20, 0.11, "Audited, frozen ETL", facecolor=COLORS["light_gray"], weight="bold")
    add_box(ax, (0.76, 0.82), 0.20, 0.11, "OMOP target", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.24, 0.875), (0.40, 0.875))
    arrow(ax, (0.60, 0.875), (0.76, 0.875))

    ax.text(0.025, 0.735, "Validation layers", fontsize=7, fontweight="bold", va="center")
    labels = [
        ("A", "Structure &\neligibility"),
        ("B", "Mapped clinical\nsemantics"),
        ("C", "Phenotype\nreproducibility"),
        ("D", "Outcome\nreproducibility"),
        ("E", "Statistics &\nprediction"),
    ]
    xs = np.linspace(0.04, 0.79, len(labels))
    box_w = 0.155
    for i, ((letter, label), x) in enumerate(zip(labels, xs)):
        face = COLORS["light_green"] if letter in {"C", "D", "E"} else "white"
        add_box(ax, (float(x), 0.61), box_w, 0.105, f"{letter}  {label}", facecolor=face, fontsize=6.7, weight="bold")
        if i < len(labels) - 1:
            arrow(ax, (float(x) + box_w, 0.662), (float(xs[i + 1]), 0.662), COLORS["mid_gray"])

    ax.text(0.025, 0.515, "Two complementary estimands", fontsize=7, fontweight="bold", va="center")
    add_box(ax, (0.04, 0.35), 0.29, 0.12, "Fixed patient + fixed index\n+ common observability", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.36, 0.35), 0.26, 0.12, "Isolate representation\nand feature construction")
    add_box(ax, (0.65, 0.35), 0.31, 0.12, "Exact acute-care outcomes;\nnear-identical logistic analyses", facecolor=COLORS["light_green"], weight="bold")
    arrow(ax, (0.33, 0.41), (0.36, 0.41), COLORS["fixed"])
    arrow(ax, (0.62, 0.41), (0.65, 0.41), COLORS["fixed"])

    add_box(ax, (0.04, 0.17), 0.29, 0.12, "Independent end-to-end\nstudy in each CDM", facecolor=COLORS["light_orange"], weight="bold")
    add_box(ax, (0.36, 0.17), 0.26, 0.12, "Includes cohort-selection\nand representation effects")
    add_box(ax, (0.65, 0.17), 0.31, 0.12, "Different cohort composition,\nrisks and model performance", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.33, 0.23), (0.36, 0.23), COLORS["end"])
    arrow(ax, (0.62, 0.23), (0.65, 0.23), COLORS["end"])

    ax.text(
        0.50,
        0.06,
        "Nonmissing DX_DATE required by ETL -> source episode excluded\n-> cohort and estimates change",
        fontsize=6.7,
        ha="center",
        va="center",
        linespacing=1.05,
    )
    return fig


def figure2_phenotype_reproducibility(data: dict) -> plt.Figure:
    primary = data["stage_c"]["primary"]
    harm = data["stage_c"]["harmonized_dxdate"]
    phenotypes = ["D0", "D1", "D3"]
    y = np.arange(3)[::-1]

    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(120)))
    gs = fig.add_gridspec(
        2, 2,
        left=0.075,
        right=0.985,
        top=0.955,
        bottom=0.095,
        hspace=0.40,
        wspace=0.30,
        height_ratios=[1.0, 1.05],
    )

    ax = fig.add_subplot(gs[0, 0])
    pc = [primary[p]["pcornet"] for p in phenotypes]
    om = [primary[p]["omop"] for p in phenotypes]
    for yi, a, b in zip(y, pc, om):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi, s=30, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=30, color=COLORS["omop"], zorder=3)
        direct_value(ax, a, yi + 0.11, f"{a:,}", COLORS["pcornet"], -3)
        direct_value(ax, b, yi - 0.11, f"{b:,}", COLORS["omop"], 3)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Patients")
    ax.set_xlim(*padded_limits(pc + om, pad_frac=0.10, min_pad=250))
    ax.set_title("Source-faithful phenotype size", loc="left")
    clean_axis(ax)
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet")
    ax.scatter([], [], color=COLORS["omop"], label="OMOP")
    ax.legend(loc="upper left", bbox_to_anchor=(0.00, 0.83), ncols=2, handletextpad=0.4, columnspacing=0.8)
    panel_label(ax, "a")

    ax = fig.add_subplot(gs[0, 1])
    jp = [primary[p]["jaccard"] for p in phenotypes]
    jh = [harm[p]["jaccard"] for p in phenotypes]
    for yi, a, b in zip(y, jp, jh):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=30, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=30, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Patient Jaccard")
    ax.set_xlim(*padded_limits(jp + jh, pad_frac=0.08, min_pad=0.012, lower=0.58, upper=1.01))
    ax.set_xticks([0.6, 0.7, 0.8, 0.9, 1.0])
    ax.set_title("Eligibility harmonization\nremoves discordance", loc="left")
    clean_axis(ax)
    ax.scatter([], [], s=26, facecolor="white", edgecolor=COLORS["primary"], label="Source-faithful")
    ax.scatter([], [], s=26, color=COLORS["dark"], label="Harmonized DX_DATE")
    ax.legend(loc="center left", bbox_to_anchor=(0.02, 0.70), handletextpad=0.4)
    panel_label(ax, "b")

    ax = fig.add_subplot(gs[1, 0])
    ip = [primary[p]["exact_index_percent"] for p in phenotypes]
    ih = [harm[p]["exact_index_percent"] for p in phenotypes]
    for yi, a, b in zip(y, ip, ih):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=30, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=30, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Exact index-date agreement\namong shared patients (%)")
    ax.set_xlim(*padded_limits(ip + ih, pad_frac=0.08, min_pad=0.15, upper=100.05))
    ax.set_xticks([97, 98, 99, 100])
    ax.set_title("Index-date agreement", loc="left")
    clean_axis(ax)
    panel_label(ax, "c")

    # The mechanism is intentionally vertical: larger text and shorter lines are more
    # readable than three horizontally compressed boxes in a half-width panel.
    ax = fig.add_subplot(gs[1, 1])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel_label(ax, "d", -0.07, 1.04)
    ax.text(0.00, 1.00, "Mechanism localized by lineage audit", fontsize=7, va="top")

    add_box(ax, (0.18, 0.73), 0.64, 0.15, "Selected stroke diagnosis\nhas null DX_DATE", facecolor=COLORS["light_orange"], fontsize=6.7, weight="bold")
    arrow(ax, (0.50, 0.73), (0.50, 0.66), COLORS["mid_gray"])
    add_box(ax, (0.18, 0.50), 0.64, 0.16, "Source uses encounter-date fallback\nFrozen ETL excludes diagnosis", fontsize=6.6)
    arrow(ax, (0.50, 0.50), (0.50, 0.43), COLORS["mid_gray"])
    add_box(ax, (0.18, 0.29), 0.64, 0.14, "No diagnosis lineage\nfor selected episode", facecolor=COLORS["light_orange"], fontsize=6.7, weight="bold")
    arrow(ax, (0.50, 0.29), (0.50, 0.22), COLORS["harmonized"])
    add_box(
        ax,
        (0.10, 0.04),
        0.80,
        0.18,
        "Symmetric nonmissing-DX_DATE eligibility\nD0/D1/D3 Jaccard = 1.000; index dates = 100% exact",
        facecolor=COLORS["light_green"],
        fontsize=6.5,
        weight="bold",
    )
    return fig


def figure3_outcome_reproducibility(data: dict) -> plt.Figure:
    d = data["stage_d"]
    labels = ["Fixed\n30 d", "Fixed\n90 d", "End-to-end\n30 d", "End-to-end\n90 d"]
    y = np.arange(4)[::-1]
    f30, f90 = d["fixed"]["30_day"], d["fixed"]["90_day"]
    e30, e90 = d["end_to_end"]["30_day"], d["end_to_end"]["90_day"]
    rows = [f30, f90, e30, e90]
    pc_risk = [r["pcornet_risk_percent"] for r in rows]
    om_risk = [r["omop_risk_percent"] for r in rows]
    diff = [r["risk_difference_pp"] for r in rows]
    rr = [r["rr"] for r in rows]
    pc_n = [f30["eligible"], f90["eligible"], e30["pcornet_eligible"], e90["pcornet_eligible"]]
    om_n = [f30["eligible"], f90["eligible"], e30["omop_eligible"], e90["omop_eligible"]]

    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(110)))
    gs = fig.add_gridspec(
        2, 2,
        left=0.125,
        right=0.985,
        top=0.94,
        bottom=0.105,
        hspace=0.40,
        wspace=0.28,
    )

    ax = fig.add_subplot(gs[0, 0])
    for yi, a, b in zip(y, pc_risk, om_risk):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=30, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=25, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)")
    ax.set_xlim(*padded_limits(pc_risk + om_risk, pad_frac=0.10, min_pad=0.7))
    ax.set_title("Observed risks", loc="left")
    clean_axis(ax)
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet")
    ax.scatter([], [], color=COLORS["omop"], label="OMOP")
    ax.legend(loc="lower right", ncols=2, handletextpad=0.4, columnspacing=0.7)
    panel_label(ax, "a", -0.24)

    ax = fig.add_subplot(gs[0, 1])
    m = float(d["equivalence_margins"]["risk_difference_pp"])
    ax.axvspan(-m, m, color=COLORS["light_green"], zorder=0)
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(diff, y, s=31, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(diff, y):
        direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlabel="OMOP - PCORnet risk\n(percentage points)")
    ax.set_xlim(*padded_limits(diff, pad_frac=0.14, min_pad=0.10, include=[-m, m, 0]))
    ax.set_title("Prespecified absolute equivalence", loc="left")
    clean_axis(ax)
    panel_label(ax, "b", -0.24)

    ax = fig.add_subplot(gs[1, 0])
    lo, hi = d["equivalence_margins"]["rr_lower"], d["equivalence_margins"]["rr_upper"]
    ax.axvspan(lo, hi, color=COLORS["light_green"], zorder=0)
    ax.axvline(1, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(rr, y, s=31, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(rr, y):
        direct_value(ax, x, yi, f"{x:.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlabel="OMOP / PCORnet risk ratio")
    ax.set_xlim(*padded_limits(rr, pad_frac=0.12, min_pad=0.012, include=[lo, hi, 1]))
    ax.set_title("Prespecified relative equivalence", loc="left")
    clean_axis(ax)
    panel_label(ax, "c", -0.24)

    ax = fig.add_subplot(gs[1, 1])
    for yi, a, b in zip(y, pc_n, om_n):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=30, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=25, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlabel="Eligible patients")
    ax.set_xlim(*padded_limits(pc_n + om_n, pad_frac=0.10, min_pad=180))
    ax.set_title("Population entering each estimand", loc="left")
    clean_axis(ax)
    panel_label(ax, "d", -0.24)
    return fig


def figure4_model_reproducibility(data: dict) -> plt.Figure:
    e = data["stage_e"]
    feature_keys = [
        "Age",
        "Female",
        "Index length of stay",
        "Prior acute-care encounters",
        "Prior all encounters",
        "Prior ischemic stroke",
    ]
    feature_labels = [
        "Age",
        "Female",
        "Index length\nof stay",
        "Prior acute-care\nencounters",
        "Prior all\nencounters",
        "Prior ischemic\nstroke",
    ]
    yf = np.arange(6)[::-1]
    fs = [abs(e["fixed_feature_smd"][f]) for f in feature_keys]
    es = [abs(e["end_to_end_feature_smd"][f]) for f in feature_keys]
    models = list(e["models"])
    short = ["Logistic", "Ridge logistic", "Gradient boosting"]
    ym = np.arange(3)[::-1]
    fa = [e["models"][m]["fixed_auroc_difference"] for m in models]
    ea = [e["models"][m]["end_auroc_difference"] for m in models]
    mad = [e["models"][m]["fixed_probability_mad"] for m in models]
    bd = [e["models"][m]["end_omop_brier"] - e["models"][m]["end_pcornet_brier"] for m in models]

    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(112)))
    gs = fig.add_gridspec(
        2, 2,
        left=0.165,
        right=0.985,
        top=0.95,
        bottom=0.11,
        hspace=0.42,
        wspace=0.31,
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.axvline(0.10, color=COLORS["mid_gray"], linewidth=0.6, linestyle="--")
    for yi, a, b in zip(yf, fs, es):
        ax.plot([a, b], [yi, yi], color=COLORS["grid"])
        ax.scatter(a, yi, s=29, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=29, color=COLORS["end"], zorder=3)
    ax.set(yticks=yf, yticklabels=feature_labels, xlabel="Absolute standardized\nmean difference")
    ax.set_xlim(*padded_limits(fs + es, pad_frac=0.10, min_pad=0.007, include=[0.10], lower=-0.002))
    ax.set_title("Feature distributions", loc="left")
    clean_axis(ax)
    ax.scatter([], [], s=25, facecolor="white", edgecolor=COLORS["fixed"], label="Fixed cohort")
    ax.scatter([], [], s=25, color=COLORS["end"], label="End-to-end")
    ax.legend(loc="lower right", handletextpad=0.4)
    panel_label(ax, "a", -0.33)

    ax = fig.add_subplot(gs[0, 1])
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    for yi, a, b in zip(ym, fa, ea):
        ax.plot([a, b], [yi, yi], color=COLORS["grid"])
        ax.scatter(a, yi, s=29, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=29, color=COLORS["end"], zorder=3)
    ax.set(yticks=ym, yticklabels=short, xlabel="AUROC difference\n(OMOP - PCORnet)")
    ax.set_xlim(*padded_limits(fa + ea, pad_frac=0.10, min_pad=0.002, include=[0]))
    ax.set_title("Model discrimination", loc="left")
    clean_axis(ax)
    panel_label(ax, "b", -0.28)

    ax = fig.add_subplot(gs[1, 0])
    ax.scatter(mad, ym, s=31, color=COLORS["fixed"], zorder=3)
    max_mad = max(mad)
    for x, yi in zip(mad, ym):
        dx = -5 if x == max_mad else 5
        direct_value(ax, x, yi, f"{x:.5f}" if x < 0.001 else f"{x:.3f}", COLORS["dark"], dx)
    ax.set(yticks=ym, yticklabels=short, xlabel="Mean absolute\nprediction difference")
    ax.set_xlim(*padded_limits(mad, pad_frac=0.14, min_pad=0.002, lower=-0.001))
    ax.set_title("Fixed-cohort individual predictions", loc="left")
    clean_axis(ax)
    panel_label(ax, "c", -0.33)

    ax = fig.add_subplot(gs[1, 1])
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(bd, ym, s=31, color=COLORS["end"], zorder=3)
    max_bd = max(bd)
    for x, yi in zip(bd, ym):
        dx = -5 if x == max_bd else 5
        direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], dx)
    ax.set(yticks=ym, yticklabels=[], xlabel="Brier difference\n(OMOP - PCORnet)")
    ax.set_xlim(*padded_limits(bd, pad_frac=0.14, min_pad=0.002, include=[0]))
    ax.set_title("End-to-end prediction error", loc="left")
    clean_axis(ax)
    panel_label(ax, "d", -0.18)
    return fig


MAIN_FIGURE_BUILDERS = {
    "Figure1_validation_framework": figure1_validation_framework,
    "Figure2_phenotype_reproducibility": figure2_phenotype_reproducibility,
    "Figure3_outcome_reproducibility": figure3_outcome_reproducibility,
    "Figure4_model_reproducibility": figure4_model_reproducibility,
}
