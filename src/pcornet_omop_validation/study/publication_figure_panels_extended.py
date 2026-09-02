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
    """Show mapped fidelity, numeric reconciliation, and unresolved coverage separately.

    The three panels intentionally use unequal widths. The prior compact layout forced
    y-axis labels from the center and right panels into neighboring panels. Here we
    reserve explicit label gutters and keep the center bar labels short enough to stay
    within their own panel while retaining the full scientific meaning in the caption.
    """
    b = data["stage_b"]
    components = list(b["mapped_exact_counts"])
    counts = [b["mapped_exact_counts"][c] for c in components]
    component_labels = [
        "Encounter",
        "Death",
        "Condition",
        "Procedure",
        "Drug",
        "Measurement/\nObservation",
    ]
    y = np.arange(len(components))[::-1]

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(98)))
    gs = fig.add_gridspec(
        1,
        3,
        left=0.15,
        right=0.985,
        top=0.90,
        bottom=0.22,
        wspace=0.58,
        width_ratios=[1.18, 1.00, 1.18],
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.scatter(counts, y, s=32, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(counts, y):
        direct_value(ax, x, yi, f"{x:,}", COLORS["dark"], 5)
    ax.set_xscale("log")
    ax.set(yticks=y, yticklabels=component_labels, xlabel="Exact mapped rows\n(log scale)")
    ax.set_xlim(min(counts) / 1.7, max(counts) * 1.7)
    ax.set_title("Mapped semantic fidelity", loc="left")
    clean_axis(ax)
    ax.text(
        0,
        -0.20,
        "100% agreement in each locked\nmapped denominator",
        transform=ax.transAxes,
        fontsize=6.7,
        va="top",
        linespacing=1.05,
    )
    panel_label(ax, "a", -0.34)

    ax = fig.add_subplot(gs[0, 1])
    n = b["numeric"]
    exact = 100 * n["direct_exact"] / n["comparable"]
    ax.barh([1], [exact], color=COLORS["fixed"], height=0.38)
    ax.barh([1], [100 - exact], left=[exact], color=COLORS["light_gray"], height=0.38)
    ax.barh([0], [100], color=COLORS["accent"], height=0.38)
    ax.set(
        xlim=(0, 104),
        yticks=[1, 0],
        yticklabels=["Directly exact\n(comparable)", "Explained\ninitial differences"],
        xlabel="Rows (%)",
    )
    ax.tick_params(axis="y", pad=4)
    ax.set_title("Numeric reconciliation", loc="left")
    clean_axis(ax)
    direct_value(ax, exact, 1, f"{exact:.3f}%", COLORS["dark"], 5)
    direct_value(ax, 100, 0, "100%", COLORS["dark"], 5)
    panel_label(ax, "b", -0.34)

    ax = fig.add_subplot(gs[0, 2])
    lim = b["coverage_limitations"]
    label_map = {
        "Condition concept-zero fallback": "Condition\nconcept-0",
        "Procedure unresolved routes": "Procedure\nunresolved",
        "Drug concept-zero routes": "Drug\nconcept-0",
        "Measurement/Observation unresolved": "Meas./obs.\nunresolved",
    }
    names = [label_map[k] for k in lim]
    vals = list(lim.values())
    yy = np.arange(len(names))[::-1]
    ax.scatter(vals, yy, s=32, color=COLORS["end"], zorder=3)
    for x, yi in zip(vals, yy):
        dx = -5 if x == max(vals) else 5
        direct_value(ax, x, yi, f"{x:,}", COLORS["dark"], dx)
    ax.set_xscale("log")
    ax.set(yticks=yy, yticklabels=names, xlabel="Rows/routes\n(log scale)")
    ax.tick_params(axis="y", pad=4)
    ax.set_xlim(min(vals) / 1.7, max(vals) * 1.7)
    ax.set_title("Coverage limitations\nkept separate", loc="left")
    clean_axis(ax)
    panel_label(ax, "c", -0.34)
    return fig


def extended_data_figure2_additional_reproducibility(data: dict) -> plt.Figure:
    """Show association, prediction, and recurrent-event sensitivity checks."""
    e = data["stage_e"]
    d = data["stage_d"]["recurrent"]
    ratios = e["fixed_association_or_ratio_omop_over_source"]
    features = list(ratios)
    feature_labels = [
        "Age",
        "Female",
        "Index length\nof stay",
        "Prior acute-care\nencounters",
        "Prior all\nencounters",
        "Prior ischemic\nstroke",
    ]
    vals = list(ratios.values())
    y = np.arange(len(features))[::-1]
    models = list(e["models"])
    short = ["Logistic", "Ridge logistic", "Gradient\nboosting"]
    corr = [e["models"][m]["fixed_probability_pearson"] for m in models]
    ym = np.arange(3)[::-1]

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(96)))
    gs = fig.add_gridspec(
        1,
        3,
        left=0.17,
        right=0.985,
        top=0.88,
        bottom=0.23,
        wspace=0.55,
        width_ratios=[1.10, 0.95, 1.12],
    )

    ax = fig.add_subplot(gs[0, 0])
    ax.axvline(1, color=COLORS["dark"], linewidth=0.6)
    ax.scatter(vals, y, s=32, color=COLORS["fixed"], zorder=3)
    ax.set(yticks=y, yticklabels=feature_labels, xlabel="OMOP / PCORnet\nodds-ratio ratio")
    ax.tick_params(axis="y", pad=4)
    ax.set_xlim(*padded_limits(vals, pad_frac=0.14, min_pad=0.00006, include=[1.0]))
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.4f"))
    ax.set_title("Fixed-cohort associations", loc="left")
    clean_axis(ax)
    panel_label(ax, "a", -0.38)

    ax = fig.add_subplot(gs[0, 1])
    ax.scatter(corr, ym, s=32, color=COLORS["fixed"], zorder=3)
    for x, yi in zip(corr, ym):
        # Put values inward from the panel boundary rather than allowing them to run
        # into the neighboring recurrent-event panel.
        dx = 6 if x < 0.98 else -6
        direct_value(ax, x, yi, f"{x:.6f}", COLORS["dark"], dx)
    ax.set(yticks=ym, yticklabels=short, xlabel="Pearson correlation")
    ax.tick_params(axis="y", pad=4)
    ax.set_xlim(*padded_limits(corr, pad_frac=0.20, min_pad=0.009, include=[1.0]))
    ax.set_title("Fixed prediction\nagreement", loc="left")
    clean_axis(ax)
    panel_label(ax, "b", -0.34)

    ax = fig.add_subplot(gs[0, 2])
    groups = ["Primary recurrent\nstroke-code endpoint", "Post-outcome\nPDX=P sensitivity"]
    yy = np.array([1, 0])
    pc = [d["primary_pcornet_events"], d["pdx_primary_sensitivity_pcornet_events"]]
    om = [d["primary_omop_events"], d["pdx_primary_sensitivity_omop_events"]]
    for yi, a, b in zip(yy, pc, om):
        ax.plot([a, b], [yi, yi], color=COLORS["mid_gray"])
        ax.scatter(a, yi + 0.035, s=32, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=0.8, zorder=3)
        ax.scatter(b, yi - 0.035, s=27, color=COLORS["omop"], zorder=4)
        if a == b:
            direct_value(ax, a, yi + 0.15, f"{a}", COLORS["dark"], 0)
        else:
            direct_value(ax, a, yi + 0.15, f"{a}", COLORS["dark"], 0)
            direct_value(ax, b, yi - 0.15, f"{b}", COLORS["dark"], 0)
    ax.set(yticks=yy, yticklabels=groups, xlabel="Patients with\nrecurrent event")
    ax.tick_params(axis="y", pad=5)
    ax.set_xlim(*padded_limits(pc + om, pad_frac=0.18, min_pad=9))
    ax.set_title("Recurrent-stroke\nsensitivity", loc="left")
    clean_axis(ax)
    panel_label(ax, "c", -0.34)

    # Keep the key completely outside the data axes so it cannot collide with points
    # or direct labels. This also makes the open/filled encoding visually consistent
    # with the main figures without consuming panel-c plotting area.
    key = [
        matplotlib.lines.Line2D(
            [], [], marker="o", linestyle="none", markerfacecolor="white",
            markeredgecolor=COLORS["pcornet"], markeredgewidth=0.8, label="PCORnet"
        ),
        matplotlib.lines.Line2D(
            [], [], marker="o", linestyle="none", markerfacecolor=COLORS["omop"],
            markeredgecolor=COLORS["omop"], label="OMOP"
        ),
    ]
    fig.legend(
        handles=key,
        loc="upper right",
        bbox_to_anchor=(0.985, 0.965),
        ncols=2,
        handletextpad=0.4,
        columnspacing=0.9,
    )
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

    fig = plt.figure(figsize=(mm(EXTENDED_DATA_WIDTH_MM), mm(78)))
    gs = fig.add_gridspec(
        1,
        2,
        left=0.13,
        right=0.985,
        top=0.88,
        bottom=0.25,
        wspace=0.28,
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
        ax.scatter(fixed["pcornet_slope"], yi + 0.16, s=30, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=4)
        ax.scatter(fixed["omop_slope"], yi + 0.10, s=24, color=COLORS["fixed"], zorder=5)
        ax.plot([end["pcornet_slope"], end["omop_slope"]], [yi - 0.13, yi - 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(end["pcornet_slope"], yi - 0.10, s=30, facecolor="white", edgecolor=COLORS["end"], linewidth=0.8, zorder=4)
        ax.scatter(end["omop_slope"], yi - 0.16, s=24, color=COLORS["end"], zorder=5)
    ax.set(yticks=y, yticklabels=labels, xlabel="Calibration slope")
    ax.set_xlim(*padded_limits(slope_values, pad_frac=0.12, min_pad=0.04, include=[1.0]))
    ax.set_title("Calibration slope", loc="left")
    clean_axis(ax)
    key = [
        matplotlib.lines.Line2D([], [], marker="o", linestyle="none", markerfacecolor="white", markeredgecolor=COLORS["dark"], markeredgewidth=0.8, label="PCORnet"),
        matplotlib.lines.Line2D([], [], marker="o", linestyle="none", markerfacecolor=COLORS["dark"], markeredgecolor=COLORS["dark"], label="OMOP"),
        matplotlib.lines.Line2D([], [], color=COLORS["fixed"], linewidth=1.0, label="Fixed cohort"),
        matplotlib.lines.Line2D([], [], color=COLORS["end"], linewidth=1.0, label="End-to-end"),
    ]
    ax.legend(handles=key, loc="upper left", bbox_to_anchor=(0.00, -0.14), ncols=2, handlelength=1.2, handletextpad=0.4, columnspacing=0.8)
    panel_label(ax, "a", -0.24)

    ax = fig.add_subplot(gs[0, 1])
    ax.axvline(0.0, color=COLORS["dark"], linewidth=0.55, linestyle=(0, (2, 2)))
    for m, yi in zip(models, y):
        fixed = cal[m]["fixed"]
        end = cal[m]["end_to_end"]
        ax.plot([fixed["pcornet_intercept"], fixed["omop_intercept"]], [yi + 0.13, yi + 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(fixed["pcornet_intercept"], yi + 0.16, s=30, facecolor="white", edgecolor=COLORS["fixed"], linewidth=0.8, zorder=4)
        ax.scatter(fixed["omop_intercept"], yi + 0.10, s=24, color=COLORS["fixed"], zorder=5)
        ax.plot([end["pcornet_intercept"], end["omop_intercept"]], [yi - 0.13, yi - 0.13], color=COLORS["mid_gray"], linewidth=0.55)
        ax.scatter(end["pcornet_intercept"], yi - 0.10, s=30, facecolor="white", edgecolor=COLORS["end"], linewidth=0.8, zorder=4)
        ax.scatter(end["omop_intercept"], yi - 0.16, s=24, color=COLORS["end"], zorder=5)
    ax.set(yticks=y, yticklabels=labels, xlabel="Calibration intercept")
    ax.set_xlim(*padded_limits(intercept_values, pad_frac=0.12, min_pad=0.04, include=[0.0]))
    ax.set_title("Calibration intercept", loc="left")
    clean_axis(ax)
    panel_label(ax, "b", -0.22)

    fig.text(
        0.50,
        0.055,
        "Ideal calibration: slope = 1; intercept = 0",
        ha="center",
        va="bottom",
        fontsize=6.7,
    )
    return fig


EXTENDED_FIGURE_BUILDERS["ExtendedDataFigure3_calibration"] = extended_data_figure3_calibration
