"""Recreate manuscript Figure 2 and Supplementary Figures S2 and S4.

Uses only publication aggregate inputs, never patient-level data. Figure 2 and
S2 read publication_figure_data_v2.json; S4 reads the 48 predictor-level
aggregate values in publication_s4_permutation_delta_v1.csv.

Example:
    python -m pcornet_omop_validation.study.publication_figure_paper2026 \
      --data study_definitions/artifacts/publication_figure_data_v2.json \
      --s4-data study_definitions/artifacts/publication_s4_permutation_delta_v1.csv \
      --output-dir results/publication_assets/figures
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

DEFAULT_AGGREGATES = Path("study_definitions/artifacts/publication_figure_data_v2.json")
DEFAULT_S4 = Path("study_definitions/artifacts/publication_s4_permutation_delta_v1.csv")
BLUE, ORANGE, GREEN = "#0072B2", "#D55E00", "#009E73"
INK, GRAY, LIGHT = "#252525", "#7F7F7F", "#D5D5D5"


def style() -> None:
    plt.rcdefaults()
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9.5,
        "axes.titlesize": 10.5, "axes.labelsize": 9.2,
        "axes.linewidth": 0.8, "pdf.fonttype": 42,
        "ps.fonttype": 42, "svg.fonttype": "none",
        "savefig.facecolor": "white",
    })


def clean(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def panel(ax, title, letter):
    ax.set_title(title, loc="left", fontweight="bold", pad=12)
    ax.text(-0.10, 1.15, letter, transform=ax.transAxes,
            fontsize=12, fontweight="bold", va="top")


def _box(ax, x, y, w, h, text, fill, border):
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.015,rounding_size=0.015",
                       edgecolor=border, facecolor=fill, linewidth=1.0,
                       transform=ax.transAxes)
    ax.add_patch(p)
    ax.text(x+w/2, y+h/2, text, transform=ax.transAxes, ha="center",
            va="center", fontsize=9.2, color=INK, linespacing=1.35)


def figure2(data: dict):
    """Source/target phenotype sizes, Jaccard, and opposite date-policy tests."""
    primary = data["stage_c"]["primary"]
    recorded = data["stage_c"]["harmonized_dxdate"]
    fallback = data["stage_c"]["encounter_date_fallback"]
    labels = ["Base stroke (D0)", "CT/MRI + lipid (D1)", "MRI + lipid (D3)"]
    keys = ("D0", "D1", "D3")
    y = np.arange(3)[::-1]
    fig = plt.figure(figsize=(11, 6.0), constrained_layout=False)
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.04], width_ratios=[1.06, 1],
                          top=.90, bottom=.10, left=.14, right=.98, wspace=.34, hspace=.55)
    a = fig.add_subplot(gs[0, 0]); b = fig.add_subplot(gs[0, 1]); c = fig.add_subplot(gs[1, :])
    for yi, key in zip(y, keys):
        p, o = primary[key]["pcornet"], primary[key]["omop"]
        a.plot((o, p), (yi, yi), lw=2.1, color=LIGHT, zorder=1)
        a.scatter(p, yi, facecolor="white", edgecolor=BLUE, linewidth=1.7, s=66, zorder=3)
        a.scatter(o, yi, color=ORANGE, s=58, zorder=3)
        a.annotate(f"{p:,}", (p, yi), xytext=(0, 11), textcoords="offset points",
                   ha="center", fontsize=9.3, fontweight="bold")
        a.annotate(f"{o:,}", (o, yi), xytext=(0, -16), textcoords="offset points",
                   ha="center", fontsize=9.3, fontweight="bold")
    a.set_xlim(4200, 10600); a.set_ylim(-.45, 2.45)
    a.set_yticks(y, labels); a.set_xlabel("Patients")
    panel(a, "Primary cohort membership", "a"); clean(a)
    a.scatter([], [], s=55, facecolor="white", edgecolor=BLUE, label="PCORnet")
    a.scatter([], [], s=55, color=ORANGE, label="OMOP")
    a.legend(loc="lower right", frameon=False, ncol=2, fontsize=8.5,
             bbox_to_anchor=(1.02, -0.39))
    for yi, key in zip(y, keys):
        j = primary[key]["jaccard"]
        assert abs(recorded[key]["jaccard"]-1.) < 1e-12
        assert abs(fallback[key]["jaccard"]-1.) < 1e-12
        b.plot([j, 1], [yi, yi], color=LIGHT, lw=2, zorder=1)
        b.scatter(j, yi, facecolor="white", edgecolor=INK, s=57, zorder=3)
        b.scatter(1, yi+.065, marker="^", color=GREEN, s=74, zorder=4)
        b.scatter(1, yi-.065, marker="s", color=ORANGE, s=48, zorder=4)
        b.annotate(f"{j:.3f}", (j,yi), xytext=(0,10), textcoords="offset points",
                   ha="center", fontsize=8.8)
    b.set_xlim(.57, 1.055); b.set_ylim(-.45,2.45)
    b.set_yticks(y, ["D0", "D1", "D3"])
    b.set_xlabel("Patient-set Jaccard similarity")
    panel(b, "Both date policies restore agreement", "b"); clean(b)
    b.scatter([], [], facecolor="white", edgecolor=INK, s=46, label="Primary")
    b.scatter([], [], color=GREEN, marker="^", s=48, label="Recorded date")
    b.scatter([], [], color=ORANGE, marker="s", s=40, label="Encounter fallback")
    b.legend(loc="lower center", bbox_to_anchor=(.5,-.49), ncol=3, fontsize=7.4,
             frameon=False, columnspacing=.9, handletextpad=.25)
    c.set_axis_off()
    c.text(.01, .96, "c", transform=c.transAxes, fontsize=12, fontweight="bold")
    c.text(.06, .96, "Diagnosis-date policy determines which source episodes survive",
           transform=c.transAxes, fontsize=11, fontweight="bold")
    lost = primary["D0"]["source_only"]
    _box(c,.26,.68,.48,.16, f"{lost:,} source-selected D0 episodes had no recorded diagnosis date\nand were absent from the original target cohort", "#F5F5F5", "#AAAAAA")
    c.annotate("", xy=(.27,.48), xytext=(.40,.68), xycoords="axes fraction",
               arrowprops={"arrowstyle":"->", "color":GRAY, "lw":1.2})
    c.annotate("", xy=(.73,.48), xytext=(.60,.68), xycoords="axes fraction",
               arrowprops={"arrowstyle":"->", "color":GRAY, "lw":1.2})
    _box(c,.04,.19,.46,.28,
         "Recorded-date restriction in both data models\nD0: 6,198 patients in each\nD0/D1/D3 membership and index dates identical",
         "#EAF6F2", GREEN)
    _box(c,.52,.19,.45,.28,
         "Encounter-admission-date fallback in OMOP\nD0: 9,815 patients in each\nD0/D1/D3 membership and index dates identical",
         "#EAF6F2", GREEN)
    c.text(.5,.04,"Only the tested diagnosis-date policy changed in each sensitivity analysis.",
           ha="center",transform=c.transAxes,fontsize=8.5,color=GRAY)
    return fig


def figure_s2(data: dict):
    """Associations plotted as (OMOP/PCORnet adjusted OR ratio)-1, centered on 0."""
    stage_e = data["stage_e"]
    feats = ["Age", "Female", "Index length of stay", "Prior acute-care encounters",
             "Prior all encounters", "Prior ischemic stroke"]
    raw_ratios = [float(stage_e["fixed_association_or_ratio_omop_over_source"][f]) for f in feats]
    assert all(.98 < v < 1.02 for v in raw_ratios)
    values = [v - 1.0 for v in raw_ratios]
    fig, ax = plt.subplots(1, 3, figsize=(11, 4.1), gridspec_kw={"width_ratios": [1.05,1,1]})
    fig.subplots_adjust(left=.18, right=.97, top=.85, bottom=.23, wspace=.52)
    a,b,c = ax
    y = np.arange(6)[::-1]
    a.axvline(0,color=GRAY,linewidth=.9)
    a.scatter(values,y,color=BLUE,s=48,zorder=3)
    a.set_yticks(y,feats); a.set_xlim(-.00085,.00064)
    a.set_xlabel("Adjusted OR ratio minus 1\n(OMOP/PCORnet)")
    panel(a,"Fixed-cohort associations","a");clean(a)
    mods = list(stage_e["models"])
    lab = ["Logistic", "Ridge logistic", "Gradient boosting"]
    corr = [stage_e["models"][m]["fixed_probability_pearson"] for m in mods]
    yy = np.arange(3)[::-1]
    b.scatter(corr,yy,color=BLUE,s=48)
    b.set_yticks(yy,lab);b.set_xlim(.935,1.008)
    b.set_xlabel("Pearson correlation")
    panel(b,"Fixed prediction agreement","b");clean(b)
    for value,yi in zip(corr,yy):
        b.annotate(">0.999" if value>.999 else f"{value:.3f}",(value,yi),
                   xytext=(-4,7), textcoords="offset points",ha="right",fontsize=8.2)
    rec=data["stage_d"]["recurrent"]
    rows=[("Recurrent-stroke\ncode endpoint",rec["primary_pcornet_events"],rec["primary_omop_events"]),
          ("Principal-diagnosis\nsensitivity",rec["pdx_primary_sensitivity_pcornet_events"],rec["pdx_primary_sensitivity_omop_events"])]
    for yi,(label,p,o) in zip([1,0],rows):
        c.scatter(p,yi+.075,s=56,facecolor="white",edgecolor=BLUE,linewidth=1.4)
        c.scatter(o,yi-.075,s=48,color=ORANGE)
        c.annotate(str(p),(p,yi+.075),xytext=(4,4),textcoords="offset points",fontsize=8)
        c.annotate(str(o),(o,yi-.075),xytext=(4,-10),textcoords="offset points",fontsize=8)
    c.set_yticks([1,0],[r[0] for r in rows]);c.set_xlim(145,285);c.set_ylim(-.38,1.38)
    c.set_xlabel("Patients with recurrent event")
    panel(c,"Recurrent-stroke sensitivity","c");clean(c)
    c.scatter([],[],facecolor="white",edgecolor=BLUE,label="PCORnet")
    c.scatter([],[],color=ORANGE,label="OMOP")
    c.legend(frameon=False,fontsize=8,ncol=2,loc="lower right",bbox_to_anchor=(1.07,-.42))
    return fig


FEATURES = [
    ("index_length_of_stay_days", "Index length of stay"),
    ("prior_365d_acute_care_encounter_count", "Prior acute-care visits"),
    ("prior_365d_all_encounter_count", "Prior all encounters"),
    ("age_at_index_years", "Age at index"),
    ("female_indicator", "Female indicator"),
    ("prior_365d_ischemic_stroke_indicator", "Prior ischemic stroke"),
]
CONTRASTS = [
    ("end_to_end", "Full PCORnet vs original OMOP", "Different held-out patients"),
    ("selection_within_pcornet", "Full vs shared PCORnet", "Same shared test patients"),
    ("representation_fixed", "Shared PCORnet vs original OMOP", "Same shared test patients"),
    ("representation_fallback", "Full PCORnet vs fallback OMOP", "Same full test patients"),
]


def read_s4(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    assert len(rows)==48, f"Expected 48 public aggregate importance rows, found {len(rows)}"
    d={}
    for r in rows:
        k=r["contrast"],r["model"],r["feature"]
        assert k not in d, f"Duplicate contrast/model/feature: {k}"
        left=float(r["left_permutation_auroc"])
        right=float(r["right_permutation_auroc"])
        delta=float(r["delta_permutation_auroc"])
        assert abs((right-left)-delta)<1e-11, (k,left,right,delta)
        d[k]=delta
    for contrast,*_ in CONTRASTS:
        for m in ("random_forest","xgboost"):
            for f,*_ in FEATURES:
                assert (contrast,m,f) in d
    return d


def figure_s4(rows):
    """Four comparison-specific attribution contrasts, common AUROC scale."""
    fig,axes=plt.subplots(2,2,figsize=(11.5,7.2),sharex=True,sharey=False)
    fig.subplots_adjust(top=.88,bottom=.13,left=.22,right=.97,hspace=.35,wspace=.20)
    for i,(contrast,heading,subtitle) in enumerate(CONTRASTS):
        ax=axes.flat[i]
        y=np.arange(6)[::-1]
        ax.axvline(0,color=GRAY,lw=.95)
        for k,(feature,flabel) in enumerate(FEATURES):
            yy=y[k]
            ax.scatter(rows[(contrast,"random_forest",feature)],yy+.11,s=48,
                       color=BLUE,marker="o",zorder=4)
            ax.scatter(rows[(contrast,"xgboost",feature)],yy-.11,s=48,
                       color=ORANGE,marker="D",zorder=4)
        ax.set_xlim(-.011,.0072);ax.set_ylim(-.6,5.6)
        ax.set_xticks([-.010, -.005, 0.0, .005])
        ax.set_yticks(y, [f[1] for f in FEATURES] if i%2==0 else [""]*6)
        ax.grid(axis="x",alpha=.15)
        ax.set_title(f"{chr(97+i)}  {heading}\n{subtitle}",fontsize=10,
                     fontweight="bold",loc="left",pad=9)
        clean(ax)
        ax.tick_params(axis="y",length=0)
    fig.supxlabel("Change in permutation importance (right minus left, AUROC units)", y=.075, fontsize=9.8)
    h=[plt.Line2D([],[],color=BLUE,marker="o",ls="None",label="Random forest"),
       plt.Line2D([],[],color=ORANGE,marker="D",ls="None",label="XGBoost")]
    fig.legend(handles=h,loc="upper center",bbox_to_anchor=(.60,.987),frameon=False,ncol=2)
    fig.text(.52,.032,"Exploratory model attribution. Differences are descriptive, not causal effects or statistical tests.",
             ha="center",fontsize=9,color=GRAY)
    return fig


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data",type=Path,default=DEFAULT_AGGREGATES)
    p.add_argument("--s4-data",type=Path,default=DEFAULT_S4)
    p.add_argument("--output-dir",type=Path,default=Path("results/publication_assets/figures"))
    p.add_argument("--only",action="append",choices=("Figure2", "SupplementaryFigureS2", "SupplementaryFigureS4"))
    p.add_argument("--dpi",type=int,default=240)
    args=p.parse_args(argv)
    data=json.loads(args.data.read_text(encoding="utf-8"))
    # Fail early when required source keys are missing. Never invent placeholders.
    for k in ("stage_c", "stage_d", "stage_e"):
        assert k in data, f"Missing actual aggregate data: {k}"
    selected=set(args.only or ["Figure2", "SupplementaryFigureS2", "SupplementaryFigureS4"])
    args.output_dir.mkdir(parents=True,exist_ok=True)
    style()
    build={
        "Figure2":lambda:figure2(data),
        "SupplementaryFigureS2":lambda:figure_s2(data),
        "SupplementaryFigureS4":lambda:figure_s4(read_s4(args.s4_data)),
    }
    for name in ("Figure2", "SupplementaryFigureS2", "SupplementaryFigureS4"):
        if name not in selected: continue
        fig=build[name]()
        for fmt in ("png", "pdf", "svg"):
            kw={"dpi":args.dpi} if fmt=="png" else {}
            fig.savefig(args.output_dir / f"{name}.{fmt}",bbox_inches="tight",**kw)
        plt.close(fig)
    print("generated",', '.join(sorted(selected)))


if __name__=="__main__":main()