from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
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


def style():
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
        "font.size": 10.2,
        "axes.titlesize": 11.2,
        "axes.labelsize": 10.2,
        "xtick.labelsize": 9.6,
        "ytick.labelsize": 9.8,
        "legend.fontsize": 9.6,
        "axes.linewidth": 0.75,
        "lines.linewidth": 1.05,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def clean(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)


def panel(ax, letter, x=-0.11, y=1.06):
    ax.text(x, y, letter, transform=ax.transAxes, fontweight="bold", fontsize=12.8, va="top")


def box(ax, xy, w, h, text, face="white", fs=10.0, bold=False, edge="#555555", radius=.015):
    x, y = xy
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0.004,rounding_size={radius}",
        facecolor=face, edgecolor=edge, linewidth=.85,
    ))
    ax.text(
        x + w / 2, y + h / 2, text,
        ha="center", va="center", fontsize=fs,
        fontweight="bold" if bold else "normal", linespacing=1.10,
    )


def arrow(ax, a, b, color="#666666"):
    ax.add_patch(FancyArrowPatch(
        a, b, arrowstyle="-|>", mutation_scale=11, linewidth=.95,
        color=color, shrinkA=0, shrinkB=0,
    ))


def figure1(data):
    c, d = data["stage_c"], data["stage_d"]
    fig = plt.figure(figsize=(10.5, 5.15))
    gs = fig.add_gridspec(
        2, 1, height_ratios=[.88, 1.12], left=.045, right=.985,
        top=.95, bottom=.07, hspace=.10,
    )

    ax = fig.add_subplot(gs[0])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "a", x=-.025, y=1.01)
    ax.text(
        .025, .99,
        "Reproducibility breaks at cohort selection, not downstream representation",
        fontsize=13.0, fontweight="bold", va="top",
    )
    xs = [.03, .29, .54, .78]
    ws = [.19, .19, .20, .19]
    texts = [
        "Mapped semantics\nexact within locked\nmapped denominators",
        f"Independent base cohort (D0)\n{c['primary']['D0']['pcornet']:,} vs {c['primary']['D0']['omop']:,}\nJaccard {c['primary']['D0']['jaccard']:.3f}",
        "Mechanism localized\nmissing diagnosis date\nfallback vs exclusion",
        "Same diagnosis-date rule\n3 phenotypes: Jaccard 1.000\nindex dates 100% exact",
    ]
    fills = [COLORS["blue_fill"], COLORS["orange_fill"], COLORS["orange_fill"], COLORS["green_fill"]]
    for i, (x, w, t, f) in enumerate(zip(xs, ws, texts, fills)):
        box(ax, (x, .29), w, .40, t, face=f, fs=9.7, bold=i in {0, 1, 3})
        if i < 3:
            arrow(ax, (x + w, .49), (xs[i + 1], .49), COLORS["mid"])
    ax.text(
        xs[1] + ws[1] / 2, .10, "BREAKPOINT", ha="center",
        fontsize=9.2, fontweight="bold", color=COLORS["omop"],
    )
    ax.text(
        xs[1] + ws[1] / 2, .17, "▲", ha="center", va="center",
        fontsize=9.0, color=COLORS["omop"],
    )

    ax = fig.add_subplot(gs[1])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "b", x=-.025, y=1.02)
    ax.text(
        .025, .98,
        "The same transformed data answer two different reproducibility questions",
        fontsize=12.0, fontweight="bold", va="top",
    )
    ax.text(.05, .81, "ESTIMAND", fontsize=9.0, fontweight="bold", color=COLORS["mid"])
    ax.text(.34, .81, "OBSERVED RESULT", fontsize=9.0, fontweight="bold", color=COLORS["mid"])
    ax.text(.74, .81, "INTERPRETATION", fontsize=9.0, fontweight="bold", color=COLORS["mid"])
    for y0, fc in [(.45, COLORS["blue_fill"]), (.07, COLORS["orange_fill"])]:
        ax.add_patch(FancyBboxPatch(
            (.025, y0), .95, .28,
            boxstyle="round,pad=.005,rounding_size=.012",
            facecolor=fc, edgecolor="none",
        ))
    ax.text(.05, .59, "FIXED patient + index\ncommon observability", fontsize=10.6, fontweight="bold", va="center", color=COLORS["pcornet"])
    ax.text(.34, .59, f"90-day risk  {d['fixed']['90_day']['pcornet_risk_percent']:.1f}% = {d['fixed']['90_day']['omop_risk_percent']:.1f}%\n1,132/1,132 first-event dates exact", fontsize=10.9, fontweight="bold", va="center")
    ax.text(.74, .59, "Representation preserved\nfor the same study anchors", fontsize=10.6, fontweight="bold", va="center", color=COLORS["green"])
    ax.text(.05, .21, "INDEPENDENT end-to-end\nstudy in each CDM", fontsize=10.6, fontweight="bold", va="center", color=COLORS["omop"])
    e = d["end_to_end"]["90_day"]
    ax.text(.34, .21, f"90-day risk  {e['pcornet_risk_percent']:.1f}% → {e['omop_risk_percent']:.1f}%\nΔ +{e['risk_difference_pp']:.2f} pp   RR {e['rr']:.2f}", fontsize=10.9, fontweight="bold", va="center")
    ax.text(.74, .21, f"Population changed\n{e['pcornet_eligible']:,} vs {e['omop_eligible']:,} eligible", fontsize=10.6, fontweight="bold", va="center", color=COLORS["omop"])
    return fig


def figure2(data):
    p, h = data["stage_c"]["primary"], data["stage_c"]["harmonized_dxdate"]
    keys = ["D0", "D1", "D3"]
    labels = {
        "D0": "Base (D0)",
        "D1": "CT/MRI + lipid (D1)",
        "D3": "MRI + lipid (D3)",
    }
    y = np.arange(3)[::-1]
    fig = plt.figure(figsize=(10.5, 5.55))
    gs = fig.add_gridspec(
        2, 2, width_ratios=[1.0, 1.15], height_ratios=[1.0, 1.15],
        left=.12, right=.985, top=.93, bottom=.11, hspace=.55, wspace=.30,
    )

    ax = fig.add_subplot(gs[0, 0])
    for yi, k in zip(y, keys):
        a, b = p[k]["pcornet"], p[k]["omop"]
        ax.plot([b, a], [yi, yi], color=COLORS["light"], lw=2.2)
        ax.scatter(a, yi, s=82, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.6, zorder=3)
        ax.scatter(b, yi, s=74, color=COLORS["omop"], zorder=3)
        ax.text(a, yi + .13, f"{a:,}", ha="center", va="bottom", fontsize=9.8)
        if k == "D3":
            ax.text(b + 150, yi + .13, f"{b:,}", ha="left", va="bottom", fontsize=9.8)
        else:
            ax.text(b, yi - .13, f"{b:,}", ha="center", va="top", fontsize=9.8)
    ax.set(yticks=y, yticklabels=[labels[k] for k in keys], xlabel="Patients")
    ax.set_xlim(4600, 10350)
    ax.set_ylim(-.30, 2.55)
    ax.set_title("Source-faithful phenotype size", loc="left", fontweight="bold", pad=12)
    clean(ax)
    panel(ax, "a", x=-.17, y=1.10)
    ax.scatter(5300, 2.34, s=54, facecolor="white", edgecolor=COLORS["pcornet"], linewidth=1.4, zorder=3)
    ax.text(5420, 2.34, "PCORnet", va="center", fontsize=9.7)
    ax.scatter(7550, 2.34, s=50, color=COLORS["omop"], zorder=3)
    ax.text(7670, 2.34, "OMOP", va="center", fontsize=9.7)

    ax = fig.add_subplot(gs[0, 1])
    for yi, k in zip(y, keys):
        a, b = p[k]["jaccard"], h[k]["jaccard"]
        ax.plot([a, b], [yi, yi], color=COLORS["light"], lw=2.2)
        ax.scatter(a, yi, s=64, facecolor="white", edgecolor=COLORS["dark"], linewidth=1.3, zorder=3)
        ax.scatter(b, yi, s=64, color=COLORS["green"], zorder=3)
        ax.text(a, yi + .11, f"{a:.3f}", ha="center", va="bottom", fontsize=9.3)
        ax.text(b, yi + .11, "1.000", ha="center", va="bottom", fontsize=9.3, fontweight="bold", color=COLORS["green"])
    ax.set(yticks=y, yticklabels=[labels[k] for k in keys], xlabel="Patient Jaccard")
    ax.set_xlim(.57, 1.03)
    ax.set_ylim(-.30, 2.30)
    ax.set_title("One eligibility rule restores exact membership", loc="left", fontweight="bold", pad=12)
    clean(ax)
    panel(ax, "b", x=-.19, y=1.10)
    ax.legend(
        handles=[
            plt.Line2D([], [], marker="o", mfc="white", mec=COLORS["dark"], ls="None", label="Source-faithful"),
            plt.Line2D([], [], marker="o", color=COLORS["green"], ls="None", label="Same diagnosis-date requirement"),
        ],
        loc="upper center", bbox_to_anchor=(.50, -.22), frameon=False,
        ncol=2, columnspacing=.9, handletextpad=.35,
    )

    ax = fig.add_subplot(gs[1, :])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")
    panel(ax, "c", x=-.055, y=1.06)
    ax.text(0, 1.02, "Mechanism localized by lineage audit", fontsize=11.3, fontweight="bold", va="bottom")
    box(ax, (.32, .78), .36, .16, "Selected stroke diagnosis has no\nrecorded diagnosis date", face=COLORS["gray_fill"], fs=10.1, bold=True)
    arrow(ax, (.50, .78), (.50, .70), COLORS["mid"])
    ax.plot([.26, .74], [.70, .70], color=COLORS["mid"], lw=.95)
    arrow(ax, (.26, .70), (.26, .60), COLORS["pcornet"])
    arrow(ax, (.74, .70), (.74, .60), COLORS["omop"])
    box(ax, (.07, .39), .38, .18, "PCORnet phenotype: encounter-date fallback\nepisode remains eligible", face=COLORS["blue_fill"], fs=10.0, bold=True, edge=COLORS["pcornet"])
    box(ax, (.55, .39), .38, .18, "Frozen transformation: diagnosis excluded\nselected episode unavailable", face=COLORS["orange_fill"], fs=10.0, bold=True, edge=COLORS["omop"])
    arrow(ax, (.26, .39), (.40, .26), COLORS["green"])
    arrow(ax, (.74, .39), (.60, .26), COLORS["green"])
    box(ax, (.23, .04), .54, .21, "Apply the same diagnosis-date requirement to both representations\nall 3 phenotypes: Jaccard 1.000 · index dates 100% exact", face=COLORS["green_fill"], fs=10.1, bold=True, edge=COLORS["green"])
    return fig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="study_definitions/artifacts/publication_figure_data_v1.json")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--figures", default="1,2,3,4", help="Comma-separated main figure numbers")
    ns = ap.parse_args()
    style()
    data = json.loads(Path(ns.data).read_text())
    out = Path(ns.outdir)
    out.mkdir(parents=True, exist_ok=True)
    requested = {x.strip() for x in ns.figures.split(",") if x.strip()}
    figs = {
        "1": ("Figure1_reproducibility_breakpoint", figure1),
        "2": ("Figure2_phenotype_mechanism", figure2),
    }
    if requested & {"3", "4"}:
        from pcornet_omop_validation.study.publication_jamia_main_v2 import figure3, figure4
        figs.update({
            "3": ("Figure3_outcome_estimands", figure3),
            "4": ("Figure4_model_reproducibility", figure4),
        })
    for num in ["1", "2", "3", "4"]:
        if num not in requested:
            continue
        name, fn = figs[num]
        fig = fn(data)
        fig.savefig(out / f"{name}.png", dpi=200, bbox_inches="tight")
        fig.savefig(out / f"{name}.pdf", bbox_inches="tight")
        plt.close(fig)


if __name__ == "__main__":
    main()
