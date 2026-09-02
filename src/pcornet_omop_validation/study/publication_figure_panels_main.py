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
    # A compact height keeps the process diagram readable when placed at full text width.
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(96)))
    ax = fig.add_axes([0.02, 0.035, 0.96, 0.94])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")

    add_box(ax, (0.04, 0.82), 0.20, 0.105, "PCORnet source", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.40, 0.82), 0.20, 0.105, "Audited, frozen ETL", facecolor=COLORS["light_gray"], weight="bold")
    add_box(ax, (0.76, 0.82), 0.20, 0.105, "OMOP target", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.24, 0.872), (0.40, 0.872))
    arrow(ax, (0.60, 0.872), (0.76, 0.872))

    ax.text(0.02, 0.73, "Validation layers", fontsize=7, fontweight="bold", va="center")
    labels = [
        ("A", "Structure &\neligibility"),
        ("B", "Mapped clinical\nsemantics"),
        ("C", "Phenotype\nreproducibility"),
        ("D", "Outcome\nreproducibility"),
        ("E", "Statistics &\nprediction"),
    ]
    xs = np.linspace(0.04, 0.78, len(labels))
    box_w = 0.16
    for i, ((letter, label), x) in enumerate(zip(labels, xs)):
        face = COLORS["light_green"] if letter in {"C", "D", "E"} else "white"
        add_box(
            ax,
            (float(x), 0.61),
            box_w,
            0.10,
            f"{letter}  {label}",
            facecolor=face,
            fontsize=6.0,
            weight="bold",
        )
        if i < len(labels) - 1:
            arrow(ax, (float(x) + box_w, 0.66), (float(xs[i + 1]), 0.66), COLORS["mid_gray"])

    ax.text(0.02, 0.515, "Two complementary estimands", fontsize=7, fontweight="bold", va="center")
    add_box(ax, (0.04, 0.36), 0.28, 0.11, "Fixed patient + fixed index\n+ common observability", facecolor=COLORS["light_blue"], weight="bold")
    add_box(ax, (0.36, 0.36), 0.25, 0.11, "Isolate representation\nand feature construction")
    add_box(ax, (0.65, 0.36), 0.31, 0.11, "Exact acute-care outcomes;\nnear-identical logistic analyses", facecolor=COLORS["light_green"], weight="bold")
    arrow(ax, (0.32, 0.415), (0.36, 0.415), COLORS["fixed"])
    arrow(ax, (0.61, 0.415), (0.65, 0.415), COLORS["fixed"])

    add_box(ax, (0.04, 0.17), 0.28, 0.11, "Independent end-to-end\nstudy in each CDM", facecolor=COLORS["light_orange"], weight="bold")
    add_box(ax, (0.36, 0.17), 0.25, 0.11, "Includes cohort-selection\nand representation effects")
    add_box(ax, (0.65, 0.17), 0.31, 0.11, "Different cohort composition,\nrisks and model performance", facecolor=COLORS["light_orange"], weight="bold")
    arrow(ax, (0.32, 0.225), (0.36, 0.225), COLORS["end"])
    arrow(ax, (0.61, 0.225), (0.65, 0.225), COLORS["end"])

    ax.text(
        0.50,
        0.055,
        "Nonmissing DX_DATE required by ETL -> source episode excluded -> cohort and estimates change",
        fontsize=6.0,
        ha="center",
        va="center",
    )
    return fig


def figure2_phenotype_reproducibility(data: dict) -> plt.Figure:
    primary = data["stage_c"]["primary"]
    harm = data["stage_c"]["harmonized_dxdate"]
    phenotypes = ["D0", "D1", "D3"]
    y = np.arange(3)[::-1]

    # Shorter canvas and tighter GridSpec reduce unused vertical/horizontal space.
    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(124)))
    gs = fig.add_gridspec(
        2, 2, left=0.075, right=0.985, top=0.955, bottom=0.085,
        hspace=0.38, wspace=0.30,
    )

    ax = fig.add_subplot(gs[0, 0])
    pc = [primary[p]["pcornet"] for p in phenotypes]
    om = [primary[p]["omop"] for p in phenotypes]
    for yi, a, b in zip(y, pc, om):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi, s=27, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=27, color=COLORS["omop"], zorder=3)
        direct_value(ax, a, yi + 0.11, f"{a:,}", COLORS["pcornet"], -2)
        direct_value(ax, b, yi - 0.11, f"{b:,}", COLORS["omop"], 2)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Patients")
    ax.set_xlim(*padded_limits(pc + om, pad_frac=0.10, min_pad=250))
    ax.set_title("Source-faithful phenotype size", loc="left")
    clean_axis(ax, "x")
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet")
    ax.scatter([], [], color=COLORS["omop"], label="OMOP")
    ax.legend(loc="lower left", ncols=2, handletextpad=0.4, columnspacing=0.9)
    panel_label(ax, "a")

    ax = fig.add_subplot(gs[0, 1])
    jp = [primary[p]["jaccard"] for p in phenotypes]
    jh = [harm[p]["jaccard"] for p in phenotypes]
    for yi, a, b in zip(y, jp, jh):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=27, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=27, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Patient Jaccard")
    ax.set_xlim(*padded_limits(jp + jh, pad_frac=0.08, min_pad=0.012, lower=0.58, upper=1.01))
    ax.set_xticks([0.6, 0.7, 0.8, 0.9, 1.0])
    ax.set_title("Eligibility harmonization removes discordance", loc="left")
    clean_axis(ax, "x")
    ax.scatter([], [], s=24, facecolor="white", edgecolor=COLORS["primary"], label="Source-faithful")
    ax.scatter([], [], s=24, color=COLORS["dark"], label="Harmonized DX_DATE")
    ax.legend(loc="center left", bbox_to_anchor=(0.02, 0.73), handletextpad=0.4)
    panel_label(ax, "b")

    ax = fig.add_subplot(gs[1, 0])
    ip = [primary[p]["exact_index_percent"] for p in phenotypes]
    ih = [harm[p]["exact_index_percent"] for p in phenotypes]
    for yi, a, b in zip(y, ip, ih):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=27, facecolor="white", edgecolor=COLORS["primary"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=27, color=COLORS["dark"], zorder=3)
    ax.set(yticks=y, yticklabels=phenotypes, xlabel="Exact index-date agreement among shared patients (%)")
    ax.set_xlim(*padded_limits(ip + ih, pad_frac=0.08, min_pad=0.15, upper=100.05))
    ax.set_xticks([97, 98, 99, 100])
    ax.set_title("Index-date agreement", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "c")

    ax = fig.add_subplot(gs[1, 1])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel_label(ax, "d", -0.07, 1.04)
    ax.text(0, 1, "Mechanism localized by lineage audit", fontsize=7, va="top")
    steps = [
        (0.01, 0.64, 0.28, "Selected stroke diagnosis\nhas null DX_DATE", COLORS["light_orange"], 5.4),
        (0.36, 0.64, 0.28, "Source: encounter-date fallback\nETL: diagnosis excluded", "white", 5.4),
        (0.71, 0.64, 0.28, "No diagnosis lineage\nfor selected episode", COLORS["light_orange"], 5.4),
    ]
    for x, yy, w, text, fc, fs in steps:
        add_box(ax, (x, yy), w, 0.20, text, facecolor=fc, fontsize=fs, weight="bold" if fc != "white" else "normal")
    arrow(ax, (0.29, 0.74), (0.36, 0.74), COLORS["mid_gray"])
    arrow(ax, (0.64, 0.74), (0.71, 0.74), COLORS["mid_gray"])
    add_box(
        ax,
        (0.10, 0.19),
        0.80,
        0.27,
        "Symmetric nonmissing-DX_DATE eligibility\nD0/D1/D3: Jaccard = 1.000\nIndex dates: 100% exact",
        facecolor=COLORS["light_green"],
        fontsize=6.0,
        weight="bold",
    )
    arrow(ax, (0.50, 0.64), (0.50, 0.47), COLORS["harmonized"])
    return fig


def figure3_outcome_reproducibility(data: dict) -> plt.Figure:
    d = data["stage_d"]
    labels = ["Fixed 30 d", "Fixed 90 d", "End-to-end 30 d", "End-to-end 90 d"]
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

    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(112)))
    gs = fig.add_gridspec(
        2, 2, left=0.095, right=0.985, top=0.955, bottom=0.10,
        hspace=0.39, wspace=0.30,
    )

    ax = fig.add_subplot(gs[0, 0])
    for yi, a, b in zip(y, pc_risk, om_risk):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=27, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=23, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)")
    ax.set_xlim(*padded_limits(pc_risk + om_risk, pad_frac=0.10, min_pad=0.7))
    ax.set_title("Observed risks", loc="left")
    clean_axis(ax, "x")
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet")
    ax.scatter([], [], color=COLORS["omop"], label="OMOP")
    ax.legend(loc="lower right", ncols=2, handletextpad=0.4, columnspacing=0.8)
    panel_label(ax, "a", -0.18)

    ax = fig.add_subplot(gs[0, 1])
    m = float(d["equivalence_margins"]["risk_difference_pp"])
    ax.axvspan(-m, m, color=COLORS["light_green"], zorder=0)
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(diff, y, s=28, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(diff, y):
        direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlabel="OMOP - PCORnet risk (percentage points)")
    ax.set_xlim(*padded_limits(diff, pad_frac=0.14, min_pad=0.10, include=[-m, m, 0]))
    ax.set_title("Prespecified absolute equivalence", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "b", -0.18)

    ax = fig.add_subplot(gs[1, 0])
    lo, hi = d["equivalence_margins"]["rr_lower"], d["equivalence_margins"]["rr_upper"]
    ax.axvspan(lo, hi, color=COLORS["light_green"], zorder=0)
    ax.axvline(1, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(rr, y, s=28, c=[COLORS["fixed"], COLORS["fixed"], COLORS["end"], COLORS["end"]], zorder=3)
    for x, yi in zip(rr, y):
        direct_value(ax, x, yi, f"{x:.3f}", COLORS["dark"], 5)
    ax.set(yticks=y, yticklabels=labels, xlabel="OMOP / PCORnet risk ratio")
    ax.set_xlim(*padded_limits(rr, pad_frac=0.12, min_pad=0.012, include=[lo, hi, 1]))
    ax.set_title("Prespecified relative equivalence", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "c", -0.18)

    ax = fig.add_subplot(gs[1, 1])
    for yi, a, b in zip(y, pc_n, om_n):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=27, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=23, color=COLORS["omop"], zorder=4)
    ax.set(yticks=y, yticklabels=labels, xlabel="Eligible patients")
    ax.set_xlim(*padded_limits(pc_n + om_n, pad_frac=0.10, min_pad=180))
    ax.set_title("Population entering each estimand", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "d", -0.18)
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
        "Index length of\nstay",
        "Prior acute-care\nencounters",
        "Prior all\nencounters",
        "Prior ischemic stroke",
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

    fig = plt.figure(figsize=(mm(DOUBLE_COLUMN_MM), mm(118)))
    gs = fig.add_gridspec(
        2, 2, left=0.145, right=0.985, top=0.955, bottom=0.10,
        hspace=0.40, wspace=0.32,
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.axvline(0.10, color=COLORS["mid_gray"], linewidth=0.6, linestyle="--")
    for yi, a, b in zip(yf, fs, es):
        ax.plot([a, b], [yi, yi], color=COLORS["grid"])
        ax.scatter(a, yi, s=25, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=25, color=COLORS["end"], zorder=3)
    ax.set(yticks=yf, yticklabels=feature_labels, xlabel="Absolute standardized mean difference")
    ax.set_xlim(*padded_limits(fs + es, pad_frac=0.10, min_pad=0.007, include=[0.10], lower=-0.002))
    ax.set_title("Feature distributions", loc="left")
    clean_axis(ax, "x")
    ax.scatter([], [], s=23, facecolor="white", edgecolor=COLORS["fixed"], label="Fixed cohort")
    ax.scatter([], [], s=23, color=COLORS["end"], label="End-to-end")
    ax.legend(loc="lower right", handletextpad=0.4)
    panel_label(ax, "a", -0.28)

    ax = fig.add_subplot(gs[0, 1])
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    for yi, a, b in zip(ym, fa, ea):
        ax.plot([a, b], [yi, yi], color=COLORS["grid"])
        ax.scatter(a, yi, s=25, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi, s=25, color=COLORS["end"], zorder=3)
    ax.set(yticks=ym, yticklabels=short, xlabel="AUROC difference (OMOP - PCORnet)")
    ax.set_xlim(*padded_limits(fa + ea, pad_frac=0.10, min_pad=0.002, include=[0]))
    ax.set_title("Model discrimination", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "b", -0.24)

    ax = fig.add_subplot(gs[1, 0])
    ax.scatter(mad, ym, s=27, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(mad, ym):
        direct_value(ax, x, yi, f"{x:.5f}" if x < 0.001 else f"{x:.3f}", COLORS["dark"], 5)
    ax.set(yticks=ym, yticklabels=short, xlabel="Mean absolute prediction difference")
    ax.set_xlim(*padded_limits(mad, pad_frac=0.10, min_pad=0.0015, lower=-0.001))
    ax.set_title("Fixed-cohort individual predictions", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "c", -0.28)

    ax = fig.add_subplot(gs[1, 1])
    ax.axvline(0, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(bd, ym, s=27, color=COLORS["end"], zorder=3)
    for x, yi in zip(bd, ym):
        direct_value(ax, x, yi, f"{x:+.3f}", COLORS["dark"], 5)
    ax.set(yticks=ym, yticklabels=short, xlabel="Brier difference (OMOP - PCORnet)")
    ax.set_xlim(*padded_limits(bd, pad_frac=0.10, min_pad=0.0015, include=[0]))
    ax.set_title("End-to-end prediction error", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "d", -0.24)
    return fig


MAIN_FIGURE_BUILDERS = {
    "Figure1_validation_framework": figure1_validation_framework,
    "Figure2_phenotype_reproducibility": figure2_phenotype_reproducibility,
    "Figure3_outcome_reproducibility": figure3_outcome_reproducibility,
    "Figure4_model_reproducibility": figure4_model_reproducibility,
}
