from __future__ import annotations

"""Generate the canonical publication figures from frozen aggregate results.

This module produces the four main figures and three extended-data figures used in
the manuscript. It reads only the versioned, disclosure-reviewed aggregate
publication artifact; no patient-level data are required.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

COLORS = {
    "pcornet": "#0072B2",
    "omop": "#D55E00",
    "green": "#009E73",
    "dark": "#222222",
    "mid": "#8A8A8A",
    "light": "#D9D9D9",
    "blue_fill": "#EAF4FA",
    "orange_fill": "#FBEDE6",
    "green_fill": "#EAF6F2",
    "gray_fill": "#F5F5F5",
}

MAIN_NAMES = (
    "Figure1_reproducibility_breakpoint",
    "Figure2_phenotype_mechanism",
    "Figure3_outcome_estimands",
    "Figure4_model_reproducibility",
)
EXTENDED_NAMES = (
    "ExtendedDataFigure1_semantic_fidelity",
    "ExtendedDataFigure2_additional_reproducibility",
    "ExtendedDataFigure3_calibration",
)
FIGURE_NAMES = MAIN_NAMES + EXTENDED_NAMES


def _available_fonts() -> set[str]:
    return {font.name for font in font_manager.fontManager.ttflist}


def resolve_font(preferred: str | None = None) -> str:
    available = _available_fonts()
    if preferred:
        if preferred not in available:
            raise RuntimeError(f"Requested font {preferred!r} is not installed")
        return preferred

    for name in ("Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"):
        if name in available:
            return name
    return "DejaVu Sans"


def apply_style(font_name: str, *, extended: bool = False) -> None:
    plt.rcdefaults()
    if extended:
        body, title, label, xtick, ytick, legend = 9.2, 10.2, 9.2, 8.7, 8.7, 8.8
    else:
        body, title, label, xtick, ytick, legend = 10.2, 11.2, 10.2, 9.6, 9.8, 9.6

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [
                font_name,
                "Arial",
                "Helvetica",
                "Liberation Sans",
                "DejaVu Sans",
            ],
            "font.size": body,
            "axes.titlesize": title,
            "axes.labelsize": label,
            "xtick.labelsize": xtick,
            "ytick.labelsize": ytick,
            "legend.fontsize": legend,
            "axes.linewidth": 0.75,
            "lines.linewidth": 1.05,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def clean(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)


def panel(ax: plt.Axes, letter: str, x: float = -0.11, y: float = 1.06) -> None:
    ax.text(
        x,
        y,
        letter,
        transform=ax.transAxes,
        fontweight="bold",
        fontsize=12.8,
        va="top",
    )


def box(
    ax: plt.Axes,
    xy: tuple[float, float],
    w: float,
    h: float,
    text: str,
    *,
    face: str = "white",
    fs: float = 10.0,
    bold: bool = False,
    edge: str = "#555555",
    radius: float = 0.015,
) -> None:
    x, y = xy
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle=f"round,pad=0.004,rounding_size={radius}",
            facecolor=face,
            edgecolor=edge,
            linewidth=0.85,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fs,
        fontweight="bold" if bold else "normal",
        linespacing=1.10,
    )


def arrow(
    ax: plt.Axes,
    a: tuple[float, float],
    b: tuple[float, float],
    color: str = "#666666",
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            a,
            b,
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=0.95,
            color=color,
            shrinkA=0,
            shrinkB=0,
        )
    )


def validate_data(data: dict) -> None:
    if data.get("version") != "publication_figure_data_v1":
        raise ValueError("Unexpected publication figure-data version")

    for phenotype in ("D0", "D1", "D3"):
        harmonized = data["stage_c"]["harmonized_dxdate"][phenotype]
        exact_membership = (
            harmonized["pcornet"]
            == harmonized["omop"]
            == harmonized["shared"]
        )
        no_discordant_members = (
            harmonized["source_only"] == 0 and harmonized["omop_only"] == 0
        )
        if not (
            exact_membership
            and no_discordant_members
            and harmonized["jaccard"] == 1.0
        ):
            raise ValueError(f"Harmonized Stage C invariant failed for {phenotype}")

    for window in ("30_day", "90_day"):
        fixed = data["stage_d"]["fixed"][window]
        if not (
            fixed["pcornet_events"] == fixed["omop_events"]
            and fixed["pcornet_risk_percent"] == fixed["omop_risk_percent"]
            and fixed["risk_difference_pp"] == 0
            and fixed["rr"] == 1
        ):
            raise ValueError(f"Fixed Stage D invariant failed for {window}")

    numeric = data["stage_b"]["numeric"]
    if (
        numeric["direct_exact"] + numeric["explained_vital_differences"]
        != numeric["comparable"]
        or numeric["unexplained"] != 0
    ):
        raise ValueError("Stage B numeric reconciliation invariant failed")


def load_data(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_data(data)
    return data


def figure1(data: dict) -> plt.Figure:
    stage_c = data["stage_c"]
    stage_d = data["stage_d"]

    fig = plt.figure(figsize=(10.5, 5.15))
    gs = fig.add_gridspec(
        2,
        1,
        height_ratios=[0.88, 1.12],
        left=0.045,
        right=0.985,
        top=0.95,
        bottom=0.07,
        hspace=0.10,
    )

    ax = fig.add_subplot(gs[0])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "a", x=-0.025, y=1.01)
    ax.text(
        0.025,
        0.99,
        "Reproducibility breaks at cohort selection, not downstream representation",
        fontsize=13.0,
        fontweight="bold",
        va="top",
    )

    xs = [0.03, 0.29, 0.54, 0.78]
    widths = [0.19, 0.19, 0.20, 0.19]
    texts = [
        "Mapped semantics\nexact within locked\nmapped denominators",
        (
            "Independent base cohort (D0)\n"
            f"{stage_c['primary']['D0']['pcornet']:,} vs "
            f"{stage_c['primary']['D0']['omop']:,}\n"
            f"Jaccard {stage_c['primary']['D0']['jaccard']:.3f}"
        ),
        "Mechanism localized\nmissing diagnosis date\nfallback vs exclusion",
        "Same diagnosis-date rule\n3 phenotypes: Jaccard 1.000\nindex dates 100% exact",
    ]
    fills = [
        COLORS["blue_fill"],
        COLORS["orange_fill"],
        COLORS["orange_fill"],
        COLORS["green_fill"],
    ]

    for i, (x, width, text, fill) in enumerate(zip(xs, widths, texts, fills)):
        box(
            ax,
            (x, 0.29),
            width,
            0.40,
            text,
            face=fill,
            fs=9.7,
            bold=i in {0, 1, 3},
        )
        if i < 3:
            arrow(ax, (x + width, 0.49), (xs[i + 1], 0.49), COLORS["mid"])

    breakpoint_x = xs[1] + widths[1] / 2
    ax.text(
        breakpoint_x,
        0.10,
        "BREAKPOINT",
        ha="center",
        fontsize=9.2,
        fontweight="bold",
        color=COLORS["omop"],
    )
    ax.text(
        breakpoint_x,
        0.17,
        "▲",
        ha="center",
        va="center",
        fontsize=9.0,
        color=COLORS["omop"],
    )

    ax = fig.add_subplot(gs[1])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "b", x=-0.025, y=1.02)
    ax.text(
        0.025,
        0.98,
        "The same transformed data answer two different reproducibility questions",
        fontsize=12.0,
        fontweight="bold",
        va="top",
    )
    ax.text(
        0.05,
        0.81,
        "ESTIMAND",
        fontsize=9.0,
        fontweight="bold",
        color=COLORS["mid"],
    )
    ax.text(
        0.34,
        0.81,
        "OBSERVED RESULT",
        fontsize=9.0,
        fontweight="bold",
        color=COLORS["mid"],
    )
    ax.text(
        0.74,
        0.81,
        "INTERPRETATION",
        fontsize=9.0,
        fontweight="bold",
        color=COLORS["mid"],
    )

    for y0, fill in [(0.45, COLORS["blue_fill"]), (0.07, COLORS["orange_fill"])]:
        ax.add_patch(
            FancyBboxPatch(
                (0.025, y0),
                0.95,
                0.28,
                boxstyle="round,pad=.005,rounding_size=.012",
                facecolor=fill,
                edgecolor="none",
            )
        )

    fixed_90 = stage_d["fixed"]["90_day"]
    ax.text(
        0.05,
        0.59,
        "FIXED patient + index\ncommon observability",
        fontsize=10.6,
        fontweight="bold",
        va="center",
        color=COLORS["pcornet"],
    )
    ax.text(
        0.34,
        0.59,
        (
            f"90-day risk  {fixed_90['pcornet_risk_percent']:.1f}% = "
            f"{fixed_90['omop_risk_percent']:.1f}%\n"
            "1,132/1,132 first-event dates exact"
        ),
        fontsize=10.9,
        fontweight="bold",
        va="center",
    )
    ax.text(
        0.74,
        0.59,
        "Representation preserved\nfor the same study anchors",
        fontsize=10.6,
        fontweight="bold",
        va="center",
        color=COLORS["green"],
    )

    end_90 = stage_d["end_to_end"]["90_day"]
    ax.text(
        0.05,
        0.21,
        "INDEPENDENT end-to-end\nstudy in each CDM",
        fontsize=10.6,
        fontweight="bold",
        va="center",
        color=COLORS["omop"],
    )
    ax.text(
        0.34,
        0.21,
        (
            f"90-day risk  {end_90['pcornet_risk_percent']:.1f}% → "
            f"{end_90['omop_risk_percent']:.1f}%\n"
            f"Δ +{end_90['risk_difference_pp']:.2f} pp   RR {end_90['rr']:.2f}"
        ),
        fontsize=10.9,
        fontweight="bold",
        va="center",
    )
    ax.text(
        0.74,
        0.21,
        (
            "Population changed\n"
            f"{end_90['pcornet_eligible']:,} vs {end_90['omop_eligible']:,} eligible"
        ),
        fontsize=10.6,
        fontweight="bold",
        va="center",
        color=COLORS["omop"],
    )
    return fig


def figure2(data: dict) -> plt.Figure:
    primary = data["stage_c"]["primary"]
    harmonized = data["stage_c"]["harmonized_dxdate"]
    keys = ["D0", "D1", "D3"]
    labels = {
        "D0": "Base (D0)",
        "D1": "CT/MRI + lipid (D1)",
        "D3": "MRI + lipid (D3)",
    }
    y = np.arange(3)[::-1]

    fig = plt.figure(figsize=(10.5, 5.55))
    gs = fig.add_gridspec(
        2,
        2,
        width_ratios=[1.0, 1.15],
        height_ratios=[1.0, 1.15],
        left=0.12,
        right=0.985,
        top=0.93,
        bottom=0.11,
        hspace=0.55,
        wspace=0.30,
    )

    ax = fig.add_subplot(gs[0, 0])
    for yi, key in zip(y, keys):
        pcornet_n = primary[key]["pcornet"]
        omop_n = primary[key]["omop"]
        ax.plot([omop_n, pcornet_n], [yi, yi], color=COLORS["light"], lw=2.2)
        ax.scatter(
            pcornet_n,
            yi,
            s=82,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.6,
            zorder=3,
        )
        ax.scatter(omop_n, yi, s=74, color=COLORS["omop"], zorder=3)
        ax.text(
            pcornet_n,
            yi + 0.13,
            f"{pcornet_n:,}",
            ha="center",
            va="bottom",
            fontsize=9.8,
        )
        if key == "D3":
            ax.text(
                omop_n + 150,
                yi + 0.13,
                f"{omop_n:,}",
                ha="left",
                va="bottom",
                fontsize=9.8,
            )
        else:
            ax.text(
                omop_n,
                yi - 0.13,
                f"{omop_n:,}",
                ha="center",
                va="top",
                fontsize=9.8,
            )

    ax.set(
        yticks=y,
        yticklabels=[labels[key] for key in keys],
        xlabel="Patients",
    )
    ax.set_xlim(4600, 10350)
    ax.set_ylim(-0.30, 2.55)
    ax.set_title("Source-faithful phenotype size", loc="left", fontweight="bold", pad=12)
    clean(ax)
    panel(ax, "a", x=-0.17, y=1.10)
    ax.scatter(
        5300,
        2.34,
        s=54,
        facecolor="white",
        edgecolor=COLORS["pcornet"],
        linewidth=1.4,
        zorder=3,
    )
    ax.text(5420, 2.34, "PCORnet", va="center", fontsize=9.7)
    ax.scatter(7550, 2.34, s=50, color=COLORS["omop"], zorder=3)
    ax.text(7670, 2.34, "OMOP", va="center", fontsize=9.7)

    ax = fig.add_subplot(gs[0, 1])
    for yi, key in zip(y, keys):
        source_jaccard = primary[key]["jaccard"]
        harmonized_jaccard = harmonized[key]["jaccard"]
        ax.plot(
            [source_jaccard, harmonized_jaccard],
            [yi, yi],
            color=COLORS["light"],
            lw=2.2,
        )
        ax.scatter(
            source_jaccard,
            yi,
            s=64,
            facecolor="white",
            edgecolor=COLORS["dark"],
            linewidth=1.3,
            zorder=3,
        )
        ax.scatter(
            harmonized_jaccard,
            yi,
            s=64,
            color=COLORS["green"],
            zorder=3,
        )
        ax.text(
            source_jaccard,
            yi + 0.11,
            f"{source_jaccard:.3f}",
            ha="center",
            va="bottom",
            fontsize=9.3,
        )
        ax.text(
            harmonized_jaccard,
            yi + 0.11,
            "1.000",
            ha="center",
            va="bottom",
            fontsize=9.3,
            fontweight="bold",
            color=COLORS["green"],
        )

    ax.set(
        yticks=y,
        yticklabels=[labels[key] for key in keys],
        xlabel="Patient Jaccard",
    )
    ax.set_xlim(0.57, 1.03)
    ax.set_ylim(-0.30, 2.30)
    ax.set_title(
        "One eligibility rule restores exact membership",
        loc="left",
        fontweight="bold",
        pad=12,
    )
    clean(ax)
    panel(ax, "b", x=-0.19, y=1.10)
    ax.legend(
        handles=[
            plt.Line2D(
                [],
                [],
                marker="o",
                mfc="white",
                mec=COLORS["dark"],
                ls="None",
                label="Source-faithful",
            ),
            plt.Line2D(
                [],
                [],
                marker="o",
                color=COLORS["green"],
                ls="None",
                label="Same diagnosis-date requirement",
            ),
        ],
        loc="upper center",
        bbox_to_anchor=(0.50, -0.22),
        frameon=False,
        ncol=2,
        columnspacing=0.9,
        handletextpad=0.35,
    )

    ax = fig.add_subplot(gs[1, :])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "c", x=-0.055, y=1.06)
    ax.text(
        0,
        1.02,
        "Mechanism localized by lineage audit",
        fontsize=11.3,
        fontweight="bold",
        va="bottom",
    )
    box(
        ax,
        (0.32, 0.78),
        0.36,
        0.16,
        "Selected stroke diagnosis has no\nrecorded diagnosis date",
        face=COLORS["gray_fill"],
        fs=10.1,
        bold=True,
    )
    arrow(ax, (0.50, 0.78), (0.50, 0.70), COLORS["mid"])
    ax.plot([0.26, 0.74], [0.70, 0.70], color=COLORS["mid"], lw=0.95)
    arrow(ax, (0.26, 0.70), (0.26, 0.60), COLORS["pcornet"])
    arrow(ax, (0.74, 0.70), (0.74, 0.60), COLORS["omop"])
    box(
        ax,
        (0.07, 0.39),
        0.38,
        0.18,
        "PCORnet phenotype: encounter-date fallback\nepisode remains eligible",
        face=COLORS["blue_fill"],
        fs=10.0,
        bold=True,
        edge=COLORS["pcornet"],
    )
    box(
        ax,
        (0.55, 0.39),
        0.38,
        0.18,
        "Frozen transformation: diagnosis excluded\nselected episode unavailable",
        face=COLORS["orange_fill"],
        fs=10.0,
        bold=True,
        edge=COLORS["omop"],
    )
    arrow(ax, (0.26, 0.39), (0.40, 0.26), COLORS["green"])
    arrow(ax, (0.74, 0.39), (0.60, 0.26), COLORS["green"])
    box(
        ax,
        (0.23, 0.04),
        0.54,
        0.21,
        (
            "Apply the same diagnosis-date requirement to both representations\n"
            "all 3 phenotypes: Jaccard 1.000 · index dates 100% exact"
        ),
        face=COLORS["green_fill"],
        fs=10.1,
        bold=True,
        edge=COLORS["green"],
    )
    return fig


def figure3(data: dict) -> plt.Figure:
    stage_d = data["stage_d"]
    labels = ["30 days", "90 days"]
    y = np.array([1, 0])

    fig = plt.figure(figsize=(9.35, 5.45))
    gs = fig.add_gridspec(
        2,
        2,
        height_ratios=[1, 1.05],
        left=0.10,
        right=0.985,
        top=0.93,
        bottom=0.12,
        hspace=0.40,
        wspace=0.22,
    )

    ax = fig.add_subplot(gs[0, 0])
    for yi, key in zip(y, ["30_day", "90_day"]):
        result = stage_d["fixed"][key]
        risk = result["pcornet_risk_percent"]
        ax.scatter(
            risk,
            yi,
            s=86,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.7,
            zorder=3,
        )
        ax.scatter(risk, yi, s=40, color=COLORS["omop"], zorder=4)
        ax.text(
            risk + 0.35,
            yi,
            f"{risk:.1f}% = {result['omop_risk_percent']:.1f}%",
            va="center",
            fontsize=10.2,
            fontweight="bold",
        )

    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)")
    ax.set_xlim(15.5, 33.3)
    clean(ax)
    panel(ax, "a")
    ax.set_title(
        "Same patient + index: outcome representation is exact",
        loc="left",
        fontweight="bold",
    )
    ax.legend(
        handles=[
            plt.Line2D(
                [],
                [],
                marker="o",
                mfc="white",
                mec=COLORS["pcornet"],
                ls="None",
                label="PCORnet",
            ),
            plt.Line2D(
                [],
                [],
                marker="o",
                color=COLORS["omop"],
                ls="None",
                label="OMOP",
            ),
        ],
        loc="center right",
        frameon=False,
        ncol=1,
    )

    ax = fig.add_subplot(gs[0, 1])
    for yi, key in zip(y, ["30_day", "90_day"]):
        result = stage_d["end_to_end"][key]
        pcornet_risk = result["pcornet_risk_percent"]
        omop_risk = result["omop_risk_percent"]
        ax.plot(
            [pcornet_risk, omop_risk],
            [yi, yi],
            color=COLORS["light"],
            lw=2.2,
        )
        ax.scatter(
            pcornet_risk,
            yi,
            s=78,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.6,
            zorder=3,
        )
        ax.scatter(omop_risk, yi, s=68, color=COLORS["omop"], zorder=3)

        text_x = max(pcornet_risk, omop_risk) + 0.40
        if key == "30_day":
            result_y, eligible_y = yi - 0.08, yi - 0.23
        else:
            result_y, eligible_y = yi + 0.10, yi - 0.08

        ax.text(
            text_x,
            result_y,
            f"Δ +{result['risk_difference_pp']:.2f} pp · RR {result['rr']:.2f}",
            ha="left",
            va="center",
            fontsize=9.5,
            fontweight="bold",
        )
        ax.text(
            text_x,
            eligible_y,
            f"eligible n: {result['pcornet_eligible']:,} vs {result['omop_eligible']:,}",
            ha="left",
            va="center",
            fontsize=9.0,
            color=COLORS["mid"],
        )

    ax.set(yticks=y, yticklabels=labels, xlabel="Acute-care risk (%)")
    ax.set_xlim(15.0, 33.3)
    ax.set_ylim(-0.18, 1.15)
    clean(ax)
    panel(ax, "b")
    ax.set_title(
        "Independent study: risk changes as the population changes",
        loc="left",
        fontweight="bold",
    )

    ax = fig.add_subplot(gs[1, :])
    comparison_labels = [
        "Fixed 30 d",
        "Fixed 90 d",
        "End-to-end 30 d",
        "End-to-end 90 d",
    ]
    yy = np.arange(4)[::-1]
    values = [
        stage_d["fixed"]["30_day"]["risk_difference_pp"],
        stage_d["fixed"]["90_day"]["risk_difference_pp"],
        stage_d["end_to_end"]["30_day"]["risk_difference_pp"],
        stage_d["end_to_end"]["90_day"]["risk_difference_pp"],
    ]
    margin = stage_d["equivalence_margins"]["risk_difference_pp"]
    ax.axvspan(-margin, margin, color=COLORS["green_fill"], zorder=0)
    ax.axvline(0, color=COLORS["dark"], lw=0.85)
    ax.scatter(
        values,
        yy,
        s=74,
        c=[
            COLORS["pcornet"],
            COLORS["pcornet"],
            COLORS["omop"],
            COLORS["omop"],
        ],
        zorder=3,
    )
    for value, yi in zip(values, yy):
        ax.text(
            value + 0.05,
            yi,
            f"{value:+.2f}",
            va="center",
            fontsize=9.8,
            fontweight="bold",
        )

    ax.set(
        yticks=yy,
        yticklabels=comparison_labels,
        xlabel="OMOP − PCORnet risk difference (percentage points)",
    )
    ax.set_xlim(-0.62, 2.10)
    clean(ax)
    panel(ax, "c", x=-0.055)
    ax.set_title(
        "Only the end-to-end estimand exceeds the prespecified reproducibility tolerance (±0.5 pp)",
        loc="left",
        fontweight="bold",
    )
    return fig


def figure4(data: dict) -> plt.Figure:
    stage_e = data["stage_e"]
    features = [
        "Age",
        "Female",
        "Index length of stay",
        "Prior acute-care encounters",
        "Prior all encounters",
        "Prior ischemic stroke",
    ]
    y = np.arange(6)[::-1]
    models = list(stage_e["models"])
    model_labels = ["Logistic", "Ridge logistic", "Gradient boosting"]
    model_y = np.arange(3)[::-1]

    fig = plt.figure(figsize=(9.8, 5.75))
    gs = fig.add_gridspec(
        2,
        2,
        width_ratios=[1.05, 1],
        left=0.19,
        right=0.985,
        top=0.92,
        bottom=0.13,
        hspace=0.40,
        wspace=0.28,
    )

    ax = fig.add_subplot(gs[:, 0])
    fixed_smd = [abs(stage_e["fixed_feature_smd"][feature]) for feature in features]
    end_smd = [
        abs(stage_e["end_to_end_feature_smd"][feature]) for feature in features
    ]
    ax.axvline(0.10, color=COLORS["mid"], ls="--", lw=0.9)
    for yi, fixed_value, end_value in zip(y, fixed_smd, end_smd):
        ax.plot(
            [fixed_value, end_value],
            [yi, yi],
            color=COLORS["light"],
            lw=2.0,
        )
        ax.scatter(
            fixed_value,
            yi,
            s=68,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.5,
            zorder=3,
        )
        ax.scatter(end_value, yi, s=62, color=COLORS["omop"], zorder=3)
        if end_value >= 0.10:
            ax.text(
                end_value + 0.006,
                yi,
                f"{end_value:.2f}",
                va="center",
                fontsize=9.4,
                fontweight="bold",
            )

    ax.set(
        yticks=y,
        yticklabels=features,
        xlabel="Absolute standardized mean difference",
    )
    ax.set_xlim(-0.005, 0.18)
    clean(ax)
    panel(ax, "a", x=-0.29, y=1.08)
    ax.tick_params(axis="y", labelsize=10.4)
    ax.set_title(
        "Case mix shifts only when cohorts are built independently",
        loc="left",
        fontweight="bold",
        pad=15,
    )
    ax.text(0.103, 5.25, "0.10 reference", fontsize=8.8, color=COLORS["mid"])
    ax.legend(
        handles=[
            plt.Line2D(
                [],
                [],
                marker="o",
                mfc="white",
                mec=COLORS["pcornet"],
                ls="None",
                label="Fixed cohort",
            ),
            plt.Line2D(
                [],
                [],
                marker="o",
                color=COLORS["omop"],
                ls="None",
                label="End-to-end",
            ),
        ],
        loc="upper right",
        bbox_to_anchor=(0.98, 0.96),
        frameon=False,
        ncol=1,
        handletextpad=0.4,
    )

    ax = fig.add_subplot(gs[0, 1])
    fixed_auc = [
        stage_e["models"][model]["fixed_auroc_difference"] for model in models
    ]
    end_auc = [
        stage_e["models"][model]["end_auroc_difference"] for model in models
    ]
    ax.axvline(0, color=COLORS["dark"], lw=0.85)
    for yi, fixed_value, end_value in zip(model_y, fixed_auc, end_auc):
        ax.plot(
            [fixed_value, end_value],
            [yi, yi],
            color=COLORS["light"],
            lw=2,
        )
        ax.scatter(
            fixed_value,
            yi,
            s=63,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.5,
            zorder=3,
        )
        ax.scatter(end_value, yi, s=58, color=COLORS["omop"], zorder=3)
        label_y = yi - 0.16 if yi > 0 else yi + 0.14
        label_va = "top" if yi > 0 else "bottom"
        ax.text(
            end_value - 0.001,
            label_y,
            f"{end_value:.2f}",
            ha="right",
            va=label_va,
            fontsize=9.3,
            fontweight="bold",
        )

    ax.set(
        yticks=model_y,
        yticklabels=model_labels,
        xlabel="AUROC difference (OMOP − PCORnet)",
    )
    ax.set_xlim(-0.052, 0.004)
    clean(ax)
    panel(ax, "b", x=-0.21, y=1.08)
    ax.tick_params(axis="y", labelsize=10.2)
    ax.set_title(
        "Discrimination is stable fixed, shifted end-to-end",
        loc="left",
        fontweight="bold",
        pad=15,
    )

    ax = fig.add_subplot(gs[1, 1])
    ax.set(xlim=(0, 1), ylim=(-0.45, 2.68))
    ax.axis("off")
    panel(ax, "c", x=-0.21, y=1.08)
    ax.text(
        0,
        2.64,
        "Individual prediction agreement vs end-to-end error",
        fontsize=11.0,
        fontweight="bold",
        va="top",
    )
    ax.text(
        0.48,
        2.22,
        "Fixed prediction MAD",
        ha="right",
        fontsize=9.2,
        fontweight="bold",
        color=COLORS["pcornet"],
    )
    ax.text(
        0.96,
        2.22,
        "End-to-end Brier Δ",
        ha="right",
        fontsize=9.2,
        fontweight="bold",
        color=COLORS["omop"],
    )

    mad = [
        stage_e["models"][model]["fixed_probability_mad"] for model in models
    ]
    brier_diff = [
        stage_e["models"][model]["end_omop_brier"]
        - stage_e["models"][model]["end_pcornet_brier"]
        for model in models
    ]
    for yi, label, mad_value, brier_value in zip(
        model_y,
        model_labels,
        mad,
        brier_diff,
    ):
        ax.text(0, yi, label, va="center", fontsize=10.0)
        ax.text(
            0.48,
            yi,
            "<0.001" if mad_value < 0.001 else f"{mad_value:.3f}",
            ha="right",
            va="center",
            fontsize=10.0,
            fontweight="bold" if mad_value < 0.001 else "normal",
            color=COLORS["pcornet"],
        )
        ax.text(
            0.96,
            yi,
            f"{brier_value:+.2f}",
            ha="right",
            va="center",
            fontsize=10.0,
            color=COLORS["omop"],
        )
        ax.plot(
            [0.03, 0.96],
            [yi - 0.30, yi - 0.30],
            color="#EEEEEE",
            lw=0.8,
        )
    return fig


def extended1(data: dict) -> plt.Figure:
    stage_b = data["stage_b"]
    fig, axs = plt.subplots(
        1,
        3,
        figsize=(10.5, 4.6),
        gridspec_kw={"width_ratios": [1.2, 1, 1.1]},
    )
    fig.subplots_adjust(
        left=0.12,
        right=0.98,
        top=0.88,
        bottom=0.18,
        wspace=0.58,
    )

    ax = axs[0]
    values = list(stage_b["mapped_exact_counts"].values())
    y = np.arange(len(values))[::-1]
    ax.scatter(values, y, s=50, color=COLORS["pcornet"])
    ax.set_xscale("log")
    ax.set_yticks(
        y,
        labels=[
            "Encounter",
            "Death",
            "Condition",
            "Procedure",
            "Drug",
            "Measurement/\nObservation",
        ],
    )
    ax.set_xlabel("Exact mapped rows (log scale)")
    ax.set_title("Mapped semantic fidelity", fontweight="bold")
    clean(ax)
    panel(ax, "a")

    ax = axs[1]
    pct = 100 * stage_b["numeric"]["direct_exact"] / stage_b["numeric"]["comparable"]
    ax.barh([1, 0], [pct, 100], height=0.55)
    ax.set_xlim(0, 104)
    ax.set_yticks(
        [1, 0],
        labels=[
            "Directly exact\namong comparable",
            "Explained among\ninitial differences",
        ],
    )
    ax.set_xlabel("Rows (%)")
    ax.set_title("Numeric reconciliation", fontweight="bold")
    clean(ax)
    ax.text(pct + 1, 1, f"{pct:.1f}%", va="center")
    ax.text(101, 0, "100%", va="center")
    panel(ax, "b")

    ax = axs[2]
    names = [
        "Condition\nunmapped",
        "Procedure\nunresolved",
        "Drug\nunmapped",
        "Measurement/\nobservation\nunresolved",
    ]
    values = list(stage_b["coverage_limitations"].values())
    yy = np.arange(4)[::-1]
    ax.scatter(values, yy, s=50, color=COLORS["omop"])
    ax.set_xscale("log")
    ax.set_yticks(yy, labels=names)
    ax.tick_params(axis="y", pad=2)
    ax.set_xlabel("Rows/routes (log scale)")
    ax.set_title("Coverage limitations kept separate", fontweight="bold")
    clean(ax)
    panel(ax, "c")
    return fig


def extended2(data: dict) -> plt.Figure:
    stage_e = data["stage_e"]
    recurrent = data["stage_d"]["recurrent"]

    fig, axs = plt.subplots(1, 3, figsize=(10.5, 4.4))
    fig.subplots_adjust(
        left=0.13,
        right=0.98,
        top=0.90,
        bottom=0.18,
        wspace=0.52,
    )

    ax = axs[0]
    features = [
        "Age",
        "Female",
        "Index length of stay",
        "Prior acute-care encounters",
        "Prior all encounters",
        "Prior ischemic stroke",
    ]
    values = [
        stage_e["fixed_association_or_ratio_omop_over_source"][feature]
        for feature in features
    ]
    y = np.arange(6)[::-1]
    ax.axvline(1, color=COLORS["dark"], lw=0.8)
    ax.scatter(values, y, s=50, color=COLORS["pcornet"])
    ax.set_yticks(y, labels=features)
    ax.set_xlim(0.9991, 1.0007)
    ax.set_xlabel("OMOP / PCORnet odds-ratio ratio")
    ax.set_title("Fixed-cohort associations", fontweight="bold", pad=15)
    clean(ax)
    panel(ax, "a")

    ax = axs[1]
    models = list(stage_e["models"])
    model_labels = ["Logistic", "Ridge logistic", "Gradient boosting"]
    values = [
        stage_e["models"][model]["fixed_probability_pearson"] for model in models
    ]
    yy = np.arange(3)[::-1]
    ax.scatter(values, yy, s=50, color=COLORS["pcornet"])
    ax.set_yticks(yy, labels=model_labels)
    ax.set_xlim(0.94, 1.002)
    ax.set_xlabel("Pearson correlation")
    ax.set_title("Fixed prediction agreement", fontweight="bold", pad=15)
    clean(ax)
    panel(ax, "b")
    for value, yi in zip(values, yy):
        if yi > 1.5 and value > 0.999:
            y_text = 1.92
            va = "top"
        else:
            y_text = yi + 0.12
            va = "baseline"
        ax.text(
            value - 0.001,
            y_text,
            ">0.999" if value > 0.999 else f"{value:.3f}",
            ha="right",
            va=va,
            fontsize=8.8,
        )

    ax = axs[2]
    labels = [
        "Primary recurrent\nstroke-code endpoint",
        "Post-outcome principal-diagnosis\nsensitivity",
    ]
    yy = np.array([1, 0])
    pcornet_events = [
        recurrent["primary_pcornet_events"],
        recurrent["pdx_primary_sensitivity_pcornet_events"],
    ]
    omop_events = [
        recurrent["primary_omop_events"],
        recurrent["pdx_primary_sensitivity_omop_events"],
    ]
    for yi, pcornet_value, omop_value in zip(yy, pcornet_events, omop_events):
        ax.scatter(
            pcornet_value,
            yi + 0.05,
            s=55,
            facecolor="white",
            edgecolor=COLORS["pcornet"],
            linewidth=1.4,
        )
        ax.scatter(
            omop_value,
            yi - 0.05,
            s=50,
            color=COLORS["omop"],
        )
        ax.text(pcornet_value + 2, yi + 0.10, str(pcornet_value), fontsize=8.8)

        if yi == 0 and omop_value == 170:
            orange_y = -0.05
            orange_x = omop_value + 6
            orange_va = "center"
        else:
            orange_y = yi - 0.16
            orange_x = omop_value + 2
            orange_va = "baseline"
        ax.text(
            orange_x,
            orange_y,
            str(omop_value),
            fontsize=8.8,
            va=orange_va,
        )

    ax.set_yticks(yy, labels=labels)
    ax.set_xlim(150, 280)
    ax.set_ylim(-0.18, 1.18)
    ax.set_xlabel("Patients with recurrent event")
    ax.set_title("Recurrent-stroke sensitivity", fontweight="bold", pad=15)
    clean(ax)
    panel(ax, "c")
    ax.legend(
        handles=[
            plt.Line2D(
                [],
                [],
                marker="o",
                mfc="white",
                mec=COLORS["pcornet"],
                ls="None",
                label="PCORnet",
            ),
            plt.Line2D(
                [],
                [],
                marker="o",
                color=COLORS["omop"],
                ls="None",
                label="OMOP",
            ),
        ],
        loc="lower right",
        frameon=False,
        handletextpad=0.4,
    )
    return fig


def extended3(data: dict) -> plt.Figure:
    calibration = data["stage_e"]["calibration"]
    models = list(calibration)
    model_labels = ["Logistic", "Ridge logistic", "Gradient boosting"]
    y = np.arange(3)[::-1]

    fig, axs = plt.subplots(1, 2, figsize=(9.5, 4.6))
    fig.subplots_adjust(
        left=0.14,
        right=0.98,
        top=0.88,
        bottom=0.20,
        wspace=0.40,
    )

    for panel_index, metric in enumerate(["slope", "intercept"]):
        ax = axs[panel_index]
        reference = 1 if metric == "slope" else 0
        ax.axvline(reference, color=COLORS["dark"], ls="--", lw=0.8)

        for yi, model in zip(y, models):
            fixed = calibration[model]["fixed"]
            end_to_end = calibration[model]["end_to_end"]
            for representation, offset in [("pcornet", 0.10), ("omop", -0.10)]:
                fixed_value = fixed[f"{representation}_{metric}"]
                end_value = end_to_end[f"{representation}_{metric}"]
                ax.plot(
                    [fixed_value, end_value],
                    [yi + offset, yi + offset],
                    color=COLORS["light"],
                    lw=2,
                )
                ax.scatter(
                    fixed_value,
                    yi + offset,
                    s=50,
                    facecolor=(
                        "white" if representation == "pcornet" else COLORS["pcornet"]
                    ),
                    edgecolor=COLORS["pcornet"],
                    linewidth=1.2,
                    zorder=3,
                )
                ax.scatter(
                    end_value,
                    yi + offset,
                    s=50,
                    facecolor=(
                        "white" if representation == "pcornet" else COLORS["omop"]
                    ),
                    edgecolor=COLORS["omop"],
                    linewidth=1.2,
                    zorder=3,
                )

        ax.set_yticks(y, labels=model_labels)
        ax.set_xlabel(f"Calibration {metric}")
        ax.set_title(f"Calibration {metric}", fontweight="bold")
        clean(ax)
        panel(ax, chr(ord("a") + panel_index))
    return fig


BUILDERS = {
    MAIN_NAMES[0]: figure1,
    MAIN_NAMES[1]: figure2,
    MAIN_NAMES[2]: figure3,
    MAIN_NAMES[3]: figure4,
    EXTENDED_NAMES[0]: extended1,
    EXTENDED_NAMES[1]: extended2,
    EXTENDED_NAMES[2]: extended3,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def save_figure(
    fig: plt.Figure,
    stem: Path,
    formats: tuple[str, ...],
    dpi: int,
) -> list[Path]:
    written: list[Path] = []
    for fmt in formats:
        path = stem.with_suffix(f".{fmt}")
        kwargs: dict[str, object] = {"bbox_inches": "tight"}
        if fmt == "png":
            kwargs["dpi"] = dpi
        fig.savefig(path, **kwargs)
        written.append(path)
    plt.close(fig)
    return written


def write_manifest(
    output_dir: Path,
    data_path: Path,
    font_name: str,
    files: list[Path],
) -> Path:
    figure_data = json.loads(data_path.read_text(encoding="utf-8"))
    payload = {
        "status": "publication_figures_complete",
        "figure_data_version": "publication_figure_data_v1",
        "frozen_etl_sha": figure_data["frozen_etl_sha"],
        "data_sha256": sha256_file(data_path),
        "script_git_sha": git_sha(),
        "matplotlib_version": matplotlib.__version__,
        "font_used": font_name,
        "aggregate_only": True,
        "figure_names": list(FIGURE_NAMES),
        "files": [
            {
                "path": path.name,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        ],
    }
    path = output_dir / "publication_figures_manifest.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the canonical publication figures"
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("study_definitions/artifacts/publication_figure_data_v1.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/publication_assets/figures"),
    )
    parser.add_argument("--formats", default="png,pdf,svg")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument(
        "--font",
        help="Preferred installed font; Arial or Helvetica recommended",
    )
    parser.add_argument(
        "--strict-font",
        action="store_true",
        help="Fail unless Arial or Helvetica is available",
    )
    parser.add_argument("--only", action="append", choices=FIGURE_NAMES)
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_data(args.data)

    if args.verify_only:
        print("status: publication_figure_data_valid")
        print(f"data_sha256: {sha256_file(args.data)}")
        return

    font_name = resolve_font(args.font)
    if args.strict_font and font_name not in {"Arial", "Helvetica"}:
        raise RuntimeError(
            "Final-submission font check failed: "
            f"resolved {font_name!r}; install Arial or Helvetica"
        )

    formats = tuple(
        item.strip().lower() for item in args.formats.split(",") if item.strip()
    )
    invalid = set(formats) - {"png", "pdf", "svg"}
    if invalid:
        raise ValueError(f"Unsupported formats: {sorted(invalid)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = set(args.only or FIGURE_NAMES)
    files: list[Path] = []

    for name in FIGURE_NAMES:
        if name not in selected:
            continue
        apply_style(font_name, extended=name in EXTENDED_NAMES)
        fig = BUILDERS[name](data)
        files.extend(save_figure(fig, args.output_dir / name, formats, args.dpi))

    manifest = write_manifest(args.output_dir, args.data, font_name, files)
    print("status: publication_figures_complete")
    print(f"font_used: {font_name}")
    print(f"figures_written: {len(files)}")
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
