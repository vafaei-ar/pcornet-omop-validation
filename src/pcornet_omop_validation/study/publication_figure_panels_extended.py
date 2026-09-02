from __future__ import annotations

"""Extended Data figures for mapped fidelity and additional analytical checks."""

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from .publication_figure_style import (
    COLORS,
    EXTENDED_DATA_WIDTH_MM,
    clean_axis,
    direct_value,
    mm,
    padded_limits,
    panel_label,
)


def extended_data_figure1_semantic_fidelity(data: dict) -> plt.Figure:
    b = data["stage_b"]
    components = list(b["mapped_exact_counts"])
    counts = [b["mapped_exact_counts"][c] for c in components]
    y = np.arange(len(components))[::-1]

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(102)))
    gs = fig.add_gridspec(
        1, 3, left=0.155, right=0.985, top=0.925, bottom=0.17,
        wspace=0.46, width_ratios=[1.05, 0.82, 1.08],
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.scatter(counts, y, s=27, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(counts, y):
        direct_value(ax, x, yi, f"{x:,}", COLORS["dark"], 4)
    ax.set_xscale("log")
    ax.set(yticks=y, yticklabels=components, xlabel="Exact mapped rows (log scale)")
    ax.set_xlim(*padded_limits(counts, pad_frac=0.18, min_pad=1))
    ax.set_title("Mapped semantic fidelity", loc="left")
    clean_axis(ax, "x")
    ax.text(0, -0.14, "100% agreement in each locked mapped denominator", transform=ax.transAxes, fontsize=5.8, va="top")
    panel_label(ax, "a", -0.34)

    ax = fig.add_subplot(gs[0, 1])
    n = b["numeric"]
    exact = 100 * n["direct_exact"] / n["comparable"]
    ax.barh([1], [exact], color=COLORS["fixed"], height=0.38)
    ax.barh([1], [100 - exact], left=[exact], color=COLORS["light_gray"], height=0.38)
    ax.barh([0], [100], color=COLORS["accent"], height=0.38)
    ax.set(
        xlim=(0, 103),
        yticks=[1, 0],
        yticklabels=["Directly exact\namong comparable", "Explained among\ninitial differences"],
        xlabel="Rows (%)",
    )
    ax.set_title("Numeric reconciliation", loc="left")
    clean_axis(ax, "x")
    direct_value(ax, exact, 1, f"{exact:.3f}%", COLORS["dark"], 4)
    direct_value(ax, 100, 0, "100%", COLORS["dark"], 4)
    panel_label(ax, "b", -0.32)

    ax = fig.add_subplot(gs[0, 2])
    lim = b["coverage_limitations"]
    label_map = {
        "Condition concept-zero fallback": "Condition concept-0",
        "Procedure unresolved routes": "Procedure unresolved",
        "Drug concept-zero routes": "Drug concept-0",
        "Measurement/Observation unresolved": "Meas./obs. unresolved",
    }
    names = [label_map[k] for k in lim]
    vals = list(lim.values())
    yy = np.arange(len(names))[::-1]
    ax.scatter(vals, yy, s=27, color=COLORS["end"], zorder=3)
    for x, yi in zip(vals, yy):
        direct_value(ax, x, yi, f"{x:,}", COLORS["dark"], -4 if x > 1e7 else 4)
    ax.set_xscale("log")
    ax.set(yticks=yy, yticklabels=names, xlabel="Rows/routes (log scale)")
    ax.set_xlim(*padded_limits(vals, pad_frac=0.20, min_pad=1))
    ax.set_title("Coverage limitations kept separate", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "c", -0.32)
    return fig


def extended_data_figure2_additional_reproducibility(data: dict) -> plt.Figure:
    e = data["stage_e"]
    d = data["stage_d"]["recurrent"]
    ratios = e["fixed_association_or_ratio_omop_over_source"]
    features = list(ratios)
    vals = list(ratios.values())
    y = np.arange(len(features))[::-1]
    models = list(e["models"])
    short = ["Logistic", "Ridge logistic", "Gradient boosting"]
    corr = [e["models"][m]["fixed_probability_pearson"] for m in models]
    ym = np.arange(3)[::-1]

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(94)))
    gs = fig.add_gridspec(
        1, 3, left=0.15, right=0.985, top=0.925, bottom=0.17,
        wspace=0.42, width_ratios=[1.05, 0.92, 1.05],
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.axvline(1, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(vals, y, s=27, color=COLORS["fixed"], zorder=3)
    ax.set(yticks=y, yticklabels=features, xlabel="OMOP / PCORnet odds-ratio ratio")
    ax.set_xlim(*padded_limits(vals, pad_frac=0.14, min_pad=0.00006, include=[1.0]))
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.4f"))
    ax.set_title("Fixed-cohort associations", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "a", -0.34)

    ax = fig.add_subplot(gs[0, 1])
    ax.scatter(corr, ym, s=27, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(corr, ym):
        direct_value(ax, x, yi, f"{x:.6f}", COLORS["dark"], -4)
    ax.set(yticks=ym, yticklabels=short, xlabel="Pearson correlation")
    ax.set_xlim(*padded_limits(corr, pad_frac=0.14, min_pad=0.008, include=[1.0]))
    ax.set_title("Fixed prediction agreement", loc="left")
    clean_axis(ax, "x")
    panel_label(ax, "b", -0.28)

    ax = fig.add_subplot(gs[0, 2])
    groups = ["Primary recurrent\nstroke-code endpoint", "Post-outcome\nPDX=P sensitivity"]
    yy = np.array([1, 0])
    pc = [d["primary_pcornet_events"], d["pdx_primary_sensitivity_pcornet_events"]]
    om = [d["primary_omop_events"], d["pdx_primary_sensitivity_omop_events"]]
    for yi, a, b in zip(yy, pc, om):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=27, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=23, color=COLORS["omop"], zorder=4)
        direct_value(ax, a, yi + 0.13, f"{a}", COLORS["pcornet"], 0)
        direct_value(ax, b, yi - 0.13, f"{b}", COLORS["omop"], 0)
    ax.set(yticks=yy, yticklabels=groups, xlabel="Patients with recurrent event")
    ax.set_xlim(*padded_limits(pc + om, pad_frac=0.14, min_pad=6))
    ax.set_title("Recurrent-stroke sensitivity", loc="left")
    clean_axis(ax, "x")
    ax.scatter([], [], facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, label="PCORnet")
    ax.scatter([], [], color=COLORS["omop"], label="OMOP")
    ax.legend(loc="upper left", ncols=2, handletextpad=0.4, columnspacing=0.8)
    panel_label(ax, "c", -0.30)
    return fig


EXTENDED_FIGURE_BUILDERS = {
    "ExtendedDataFigure1_semantic_fidelity": extended_data_figure1_semantic_fidelity,
    "ExtendedDataFigure2_additional_reproducibility": extended_data_figure2_additional_reproducibility,
}


def extended_data_figure3_calibration(data: dict) -> plt.Figure:
    """Show calibration slope/intercept under fixed and end-to-end estimands."""
    cal = data["stage_e"]["calibration"]
    models = list(cal)
    labels = ["Logistic", "Ridge logistic", "Gradient boosting"]
    y = np.arange(len(models))[::-1]

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(82)))
    gs = fig.add_gridspec(
        1, 2, left=0.12, right=0.985, top=0.90, bottom=0.22,
        wspace=0.30,
    )

    slope_values: list[float] = []
    intercept_values: list[float] = []
    for m in models:
        for block in (cal[m]["fixed"], cal[m]["end_to_end"]):
            slope_values.extend([block["pcornet_slope"], block["omop_slope"]])
            intercept_values.extend([block["pcornet_intercept"], block["omop_intercept"]])

    ax = fig.add_subplot(gs[0, 0])
    ax.axvline(1.0, color=COLORS["dark"], linewidth=0.55, linestyle=(0, (2, 2)))
    for m, yi in zip(models, y):
        fixed = cal[m]["fixed"]
        end = cal[m]["end_to_end"]
        ax.plot([fixed["pcornet_slope"], fixed["omop_slope"]], [yi + 0.13, yi + 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(fixed["pcornet_slope"], yi + 0.16, s=26, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=4)
        ax.scatter(fixed["omop_slope"], yi + 0.10, s=21, color=COLORS["fixed"], zorder=5)
        ax.plot([end["pcornet_slope"], end["omop_slope"]], [yi - 0.13, yi - 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(end["pcornet_slope"], yi - 0.10, s=26, facecolor="white", edgecolor=COLORS["end"], linewidth=0.8, zorder=4)
        ax.scatter(end["omop_slope"], yi - 0.16, s=21, color=COLORS["end"], zorder=5)
    ax.set(yticks=y, yticklabels=labels, xlabel="Calibration slope")
    ax.set_xlim(*padded_limits(slope_values, pad_frac=0.12, min_pad=0.04, include=[1.0]))
    ax.set_title("Calibration slope", loc="left")
    clean_axis(ax, "x")
    key = [
        matplotlib.lines.Line2D([], [], marker="o", linestyle="none", markerfacecolor="white", markeredgecolor=COLORS["dark"], markeredgewidth=0.8, label="PCORnet"),
        matplotlib.lines.Line2D([], [], marker="o", linestyle="none", markerfacecolor=COLORS["dark"], markeredgecolor=COLORS["dark"], label="OMOP"),
        matplotlib.lines.Line2D([], [], color=COLORS["fixed"], linewidth=1.0, label="Fixed cohort"),
        matplotlib.lines.Line2D([], [], color=COLORS["end"], linewidth=1.0, label="End-to-end"),
    ]
    ax.legend(handles=key, loc="upper left", bbox_to_anchor=(0.00, -0.12), ncols=2, handlelength=1.2, handletextpad=0.4, columnspacing=0.8)
    panel_label(ax, "a", -0.22)

    ax = fig.add_subplot(gs[0, 1])
    ax.axvline(0.0, color=COLORS["dark"], linewidth=0.55, linestyle=(0, (2, 2)))
    for m, yi in zip(models, y):
        fixed = cal[m]["fixed"]
        end = cal[m]["end_to_end"]
        ax.plot([fixed["pcornet_intercept"], fixed["omop_intercept"]], [yi + 0.13, yi + 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(fixed["pcornet_intercept"], yi + 0.16, s=26, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=4)
        ax.scatter(fixed["omop_intercept"], yi + 0.10, s=21, color=COLORS["fixed"], zorder=5)
        ax.plot([end["pcornet_intercept"], end["omop_intercept"]], [yi - 0.13, yi - 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(end["pcornet_intercept"], yi - 0.10, s=26, facecolor="white", edgecolor=COLORS["end"], linewidth=0.8, zorder=4)
        ax.scatter(end["omop_intercept"], yi - 0.16, s=21, color=COLORS["end"], zorder=5)
    ax.set(yticks=y, yticklabels=labels, xlabel="Calibration intercept")
    ax.set_xlim(*padded_limits(intercept_values, pad_frac=0.12, min_pad=0.04, include=[0.0]))
    ax.set_title("Calibration intercept", loc="left")
    clean_axis(ax, "x")
    ax.text(0.02, -0.22, "Ideal calibration: slope = 1, intercept = 0", transform=ax.transAxes, fontsize=5.8, va="top")
    panel_label(ax, "b", -0.22)
    return fig


EXTENDED_FIGURE_BUILDERS["ExtendedDataFigure3_calibration"] = extended_data_figure3_calibration
