from __future__ import annotations
import argparse, json
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

COLORS = {
    "pcornet": "#0072B2", "omop": "#D55E00", "green": "#009E73",
    "dark": "#222222", "mid": "#8A8A8A", "light": "#D9D9D9",
    "blue_fill": "#EAF4FA", "orange_fill": "#FBEDE6", "green_fill": "#EAF6F2",
    "gray_fill": "#F5F5F5"
}

def style():
    plt.rcParams.update({
        "font.family":"sans-serif", "font.sans-serif":["Arial","Helvetica","Liberation Sans","DejaVu Sans"],
        "font.size":10.2, "axes.titlesize":11.2, "axes.labelsize":10.2,
        "xtick.labelsize":9.6, "ytick.labelsize":9.8, "legend.fontsize":9.6,
        "axes.linewidth":0.75, "lines.linewidth":1.05, "pdf.fonttype":42, "ps.fonttype":42,
        "figure.facecolor":"white", "axes.facecolor":"white"
    })

def clean(ax):
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False); ax.grid(False)

def panel(ax, letter, x=-0.11, y=1.06):
    ax.text(x,y,letter,transform=ax.transAxes,fontweight="bold",fontsize=12.8,va="top")

def box(ax, xy, w, h, text, face="white", fs=10.0, bold=False, edge="#555555", radius=.015):
    x,y=xy
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle=f"round,pad=0.004,rounding_size={radius}",facecolor=face,edgecolor=edge,linewidth=.85))
    ax.text(x+w/2,y+h/2,text,ha="center",va="center",fontsize=fs,fontweight="bold" if bold else "normal",linespacing=1.10)

def arrow(ax,a,b,color="#666666"):
    ax.add_patch(FancyArrowPatch(a,b,arrowstyle="-|>",mutation_scale=11,linewidth=.95,color=color,shrinkA=0,shrinkB=0))

def figure1(data):
    c,d=data["stage_c"],data["stage_d"]
    fig=plt.figure(figsize=(10.5,5.15))
    gs=fig.add_gridspec(2,1,height_ratios=[.88,1.12],left=.045,right=.985,top=.95,bottom=.07,hspace=.10)
    ax=fig.add_subplot(gs[0]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off")
    panel(ax,"a",x=-.025,y=1.01)
    ax.text(.025,.99,"Reproducibility breaks at cohort selection, not downstream representation",fontsize=13.0,fontweight="bold",va="top")
    xs=[.03,.29,.54,.79]; ws=[.19,.19,.19,.18]
    texts=[
        "Mapped semantics\nexact within locked\nmapped denominators",
        f"Independent D0 cohort\n{c['primary']['D0']['pcornet']:,} vs {c['primary']['D0']['omop']:,}\nJaccard {c['primary']['D0']['jaccard']:.3f}",
        "Mechanism localized\nmissing DX_DATE\nsource fallback vs ETL exclusion",
        "Harmonized eligibility\nD0/D1/D3 Jaccard 1.000\nindex dates 100% exact"
    ]
    fills=[COLORS["blue_fill"],COLORS["orange_fill"],COLORS["orange_fill"],COLORS["green_fill"]]
    for i,(x,w,t,f) in enumerate(zip(xs,ws,texts,fills)):
        box(ax,(x,.29),w,.40,t,face=f,fs=9.7,bold=i in {0,1,3})
        if i<3: arrow(ax,(x+w,.49),(xs[i+1],.49),COLORS["mid"])
    ax.text(xs[1]+ws[1]/2,.10,"BREAKPOINT",ha="center",fontsize=9.2,fontweight="bold",color=COLORS["omop"])
    ax.text(xs[1]+ws[1]/2,.17,"▲",ha="center",va="center",fontsize=9.0,color=COLORS["omop"])

    ax=fig.add_subplot(gs[1]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off")
    panel(ax,"b",x=-.025,y=1.02)
    ax.text(.025,.98,"The same transformed data answer two different reproducibility questions",fontsize=12.0,fontweight="bold",va="top")
    ax.text(.05,.81,"ESTIMAND",fontsize=9.0,fontweight="bold",color=COLORS["mid"])
    ax.text(.34,.81,"OBSERVED RESULT",fontsize=9.0,fontweight="bold",color=COLORS["mid"])
    ax.text(.74,.81,"INTERPRETATION",fontsize=9.0,fontweight="bold",color=COLORS["mid"])
    for y0,fc in [(.45,COLORS["blue_fill"]),(.07,COLORS["orange_fill"])]:
        ax.add_patch(FancyBboxPatch((.025,y0),.95,.28,boxstyle="round,pad=.005,rounding_size=.012",facecolor=fc,edgecolor="none"))
    ax.text(.05,.59,"FIXED patient + index\ncommon observability",fontsize=10.6,fontweight="bold",va="center",color=COLORS["pcornet"])
    ax.text(.34,.59,f"90-day risk  {d['fixed']['90_day']['pcornet_risk_percent']:.1f}% = {d['fixed']['90_day']['omop_risk_percent']:.1f}%\n1,132/1,132 first-event dates exact",fontsize=10.9,fontweight="bold",va="center")
    ax.text(.74,.59,"Representation preserved\nfor the same study anchors",fontsize=10.6,fontweight="bold",va="center",color=COLORS["green"])
    ax.text(.05,.21,"INDEPENDENT end-to-end\nstudy in each CDM",fontsize=10.6,fontweight="bold",va="center",color=COLORS["omop"])
    e=d["end_to_end"]["90_day"]
    ax.text(.34,.21,f"90-day risk  {e['pcornet_risk_percent']:.1f}% → {e['omop_risk_percent']:.1f}%\nΔ +{e['risk_difference_pp']:.2f} pp   RR {e['rr']:.2f}",fontsize=10.9,fontweight="bold",va="center")
    ax.text(.74,.21,f"Population changed\n{e['pcornet_eligible']:,} vs {e['omop_eligible']:,} eligible",fontsize=10.6,fontweight="bold",va="center",color=COLORS["omop"])
    return fig

def figure2(data):
    p,h=data["stage_c"]["primary"],data["stage_c"]["harmonized_dxdate"]
    ph=["D0","D1","D3"]; y=np.arange(3)[::-1]
    fig=plt.figure(figsize=(10.5,5.55))
    gs=fig.add_gridspec(2,2,width_ratios=[1.0,1.15],height_ratios=[1.0,1.15],left=.09,right=.985,top=.93,bottom=.11,hspace=.55,wspace=.28)

    ax=fig.add_subplot(gs[0,0])
    for yi,k in zip(y,ph):
        a,b=p[k]["pcornet"],p[k]["omop"]
        ax.plot([b,a],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=82,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.6,zorder=3)
        ax.scatter(b,yi,s=74,color=COLORS["omop"],zorder=3)
        # Keep direct labels close enough to associate with points, but not visually touching them.
        ax.text(a,yi+.13,f"{a:,}",ha="center",va="bottom",fontsize=9.8)
        if k == "D3":
            ax.text(b+150,yi+.13,f"{b:,}",ha="left",va="bottom",fontsize=9.8)
        else:
            ax.text(b,yi-.13,f"{b:,}",ha="center",va="top",fontsize=9.8)
    ax.set(yticks=y,yticklabels=ph,xlabel="Patients"); ax.set_xlim(4600,10350); ax.set_ylim(-.30,2.55)
    ax.set_title("Source-faithful phenotype size",loc="left",fontweight="bold",pad=12); clean(ax); panel(ax,"a",x=-.13,y=1.10)
    ax.scatter(5300,2.34,s=54,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.4,zorder=3)
    ax.text(5420,2.34,"PCORnet",va="center",fontsize=9.7)
    ax.scatter(7550,2.34,s=50,color=COLORS["omop"],zorder=3)
    ax.text(7670,2.34,"OMOP",va="center",fontsize=9.7)

    ax=fig.add_subplot(gs[0,1])
    for yi,k in zip(y,ph):
        a,b=p[k]["jaccard"],h[k]["jaccard"]
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=64,facecolor="white",edgecolor=COLORS["dark"],linewidth=1.3,zorder=3)
        ax.scatter(b,yi,s=64,color=COLORS["green"],zorder=3)
        ax.text(a,yi+.11,f"{a:.3f}",ha="center",va="bottom",fontsize=9.3)
        ax.text(b,yi+.11,"1.000",ha="center",va="bottom",fontsize=9.3,fontweight="bold",color=COLORS["green"])
    ax.set(yticks=y,yticklabels=ph,xlabel="Patient Jaccard"); ax.set_xlim(.57,1.03); ax.set_ylim(-.30,2.30)
    ax.set_title("One eligibility rule restores exact membership",loc="left",fontweight="bold",pad=12); clean(ax); panel(ax,"b",x=-.11,y=1.10)
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["dark"],ls="None",label="Source-faithful"),plt.Line2D([],[],marker="o",color=COLORS["green"],ls="None",label="Symmetric nonmissing DX_DATE")],loc="upper center",bbox_to_anchor=(.50,-.22),frameon=False,ncol=2,columnspacing=.9,handletextpad=.35)

    ax=fig.add_subplot(gs[1,:]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off"); panel(ax,"c",x=-.045,y=1.06)
    ax.text(0,1.02,"Mechanism localized by lineage audit",fontsize=11.3,fontweight="bold",va="bottom")
    box(ax,(.32,.78),.36,.16,"Selected stroke diagnosis: DX_DATE missing",face=COLORS["gray_fill"],fs=10.1,bold=True)
    arrow(ax,(.50,.78),(.50,.70),COLORS["mid"])
    ax.plot([.26,.74],[.70,.70],color=COLORS["mid"],lw=.95)
    arrow(ax,(.26,.70),(.26,.60),COLORS["pcornet"]); arrow(ax,(.74,.70),(.74,.60),COLORS["omop"])
    box(ax,(.07,.39),.38,.18,"PCORnet phenotype: encounter-date fallback\nepisode remains eligible",face=COLORS["blue_fill"],fs=10.0,bold=True,edge=COLORS["pcornet"])
    box(ax,(.55,.39),.38,.18,"Frozen ETL: no diagnosis event materialized\nselected episode absent",face=COLORS["orange_fill"],fs=10.0,bold=True,edge=COLORS["omop"])
    arrow(ax,(.26,.39),(.40,.26),COLORS["green"]); arrow(ax,(.74,.39),(.60,.26),COLORS["green"])
    box(ax,(.23,.04),.54,.21,"Require nonmissing DX_DATE in both representations\nD0/D1/D3: Jaccard 1.000 · index dates 100% exact",face=COLORS["green_fill"],fs=10.1,bold=True,edge=COLORS["green"])
    return fig

def figure3(data):
    d=data["stage_d"]; labels=["30 days","90 days"]; y=np.array([1,0])
    fig=plt.figure(figsize=(9.35,5.45)); gs=fig.add_gridspec(2,2,height_ratios=[1,1.05],left=.10,right=.985,top=.93,bottom=.12,hspace=.40,wspace=.22)
    ax=fig.add_subplot(gs[0,0])
    for yi,key in zip(y,["30_day","90_day"]):
        r=d["fixed"][key]; x=r["pcornet_risk_percent"]
        ax.scatter(x,yi,s=86,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.7,zorder=3)
        ax.scatter(x,yi,s=40,color=COLORS["omop"],zorder=4)
        ax.text(x+.35,yi,f"{x:.1f}% = {r['omop_risk_percent']:.1f}%",va="center",fontsize=10.2,fontweight="bold")
    ax.set(yticks=y,yticklabels=labels,xlabel="Acute-care risk (%)"); ax.set_xlim(15.5,33.3); clean(ax); panel(ax,"a")
    ax.set_title("Same patient + index: outcome representation is exact",loc="left",fontweight="bold")
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["pcornet"],ls="None",label="PCORnet"),plt.Line2D([],[],marker="o",color=COLORS["omop"],ls="None",label="OMOP")],loc="center right",frameon=False,ncol=1)

    ax=fig.add_subplot(gs[0,1])
    for yi,key in zip(y,["30_day","90_day"]):
        r=d["end_to_end"][key]; a,b=r["pcornet_risk_percent"],r["omop_risk_percent"]
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=78,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.6,zorder=3)
        ax.scatter(b,yi,s=68,color=COLORS["omop"],zorder=3)
        tx=max(a,b)+.40
        if key == "30_day": ay1, ay2 = yi-.08, yi-.23
        else: ay1, ay2 = yi+.10, yi-.08
        ax.text(tx,ay1,f"Δ +{r['risk_difference_pp']:.2f} pp · RR {r['rr']:.2f}",ha="left",va="center",fontsize=9.5,fontweight="bold")
        ax.text(tx,ay2,f"eligible n: {r['pcornet_eligible']:,} vs {r['omop_eligible']:,}",ha="left",va="center",fontsize=9.0,color=COLORS["mid"])
    ax.set(yticks=y,yticklabels=labels,xlabel="Acute-care risk (%)"); ax.set_xlim(15.0,33.3); ax.set_ylim(-.18,1.15); clean(ax); panel(ax,"b")
    ax.set_title("Independent study: risk changes as the population changes",loc="left",fontweight="bold")

    ax=fig.add_subplot(gs[1,:]); labs=["Fixed 30 d","Fixed 90 d","End-to-end 30 d","End-to-end 90 d"]; yy=np.arange(4)[::-1]
    vals=[d["fixed"]["30_day"]["risk_difference_pp"],d["fixed"]["90_day"]["risk_difference_pp"],d["end_to_end"]["30_day"]["risk_difference_pp"],d["end_to_end"]["90_day"]["risk_difference_pp"]]
    m=d["equivalence_margins"]["risk_difference_pp"]
    ax.axvspan(-m,m,color=COLORS["green_fill"],zorder=0); ax.axvline(0,color=COLORS["dark"],lw=.85)
    ax.scatter(vals,yy,s=74,c=[COLORS["pcornet"],COLORS["pcornet"],COLORS["omop"],COLORS["omop"]],zorder=3)
    for x,yi in zip(vals,yy): ax.text(x+.05,yi,f"{x:+.2f}",va="center",fontsize=9.8,fontweight="bold")
    ax.set(yticks=yy,yticklabels=labs,xlabel="OMOP − PCORnet risk difference (percentage points)"); ax.set_xlim(-.62,2.10); clean(ax); panel(ax,"c",x=-.055)
    ax.set_title("Only the end-to-end estimand exceeds the prespecified reproducibility tolerance (±0.5 pp)",loc="left",fontweight="bold")
    return fig

def figure4(data):
    e=data["stage_e"]
    features=["Age","Female","Index length of stay","Prior acute-care encounters","Prior all encounters","Prior ischemic stroke"]
    y=np.arange(6)[::-1]; models=list(e["models"]); mlabs=["Logistic","Ridge logistic","Gradient boosting"]; ym=np.arange(3)[::-1]
    fig=plt.figure(figsize=(9.8,5.75)); gs=fig.add_gridspec(2,2,width_ratios=[1.05,1],left=.19,right=.985,top=.92,bottom=.13,hspace=.40,wspace=.28)
    ax=fig.add_subplot(gs[:,0]); fs=[abs(e["fixed_feature_smd"][x]) for x in features]; es=[abs(e["end_to_end_feature_smd"][x]) for x in features]
    ax.axvline(.10,color=COLORS["mid"],ls="--",lw=.9)
    for yi,a,b in zip(y,fs,es):
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.0)
        ax.scatter(a,yi,s=68,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.5,zorder=3)
        ax.scatter(b,yi,s=62,color=COLORS["omop"],zorder=3)
        if b>=.10: ax.text(b+.006,yi,f"{b:.2f}",va="center",fontsize=9.4,fontweight="bold")
    ax.set(yticks=y,yticklabels=features,xlabel="Absolute standardized mean difference"); ax.set_xlim(-.005,.18); clean(ax); panel(ax,"a",x=-.29,y=1.08)
    ax.tick_params(axis="y",labelsize=10.4)
    ax.set_title("Case mix shifts only when cohorts are built independently",loc="left",fontweight="bold",pad=15)
    ax.text(.103,5.25,"0.10 reference",fontsize=8.8,color=COLORS["mid"])
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["pcornet"],ls="None",label="Fixed cohort"),plt.Line2D([],[],marker="o",color=COLORS["omop"],ls="None",label="End-to-end")],loc="upper right",bbox_to_anchor=(.98,.96),frameon=False,ncol=1,handletextpad=.4)

    ax=fig.add_subplot(gs[0,1]); fa=[e["models"][m]["fixed_auroc_difference"] for m in models]; ea=[e["models"][m]["end_auroc_difference"] for m in models]
    ax.axvline(0,color=COLORS["dark"],lw=.85)
    for yi,a,b in zip(ym,fa,ea):
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2)
        ax.scatter(a,yi,s=63,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.5,zorder=3)
        ax.scatter(b,yi,s=58,color=COLORS["omop"],zorder=3)
        label_y = yi-.16 if yi > 0 else yi+.14
        label_va = "top" if yi > 0 else "bottom"
        ax.text(b-.001,label_y,f"{b:.2f}",ha="right",va=label_va,fontsize=9.3,fontweight="bold")
    ax.set(yticks=ym,yticklabels=mlabs,xlabel="AUROC difference (OMOP − PCORnet)"); ax.set_xlim(-.052,.004); clean(ax); panel(ax,"b",x=-.21,y=1.08)
    ax.tick_params(axis="y",labelsize=10.2)
    ax.set_title("Discrimination is stable fixed, shifted end-to-end",loc="left",fontweight="bold",pad=15)

    ax=fig.add_subplot(gs[1,1]); ax.set(xlim=(0,1),ylim=(-.45,2.68)); ax.axis("off"); panel(ax,"c",x=-.21,y=1.08)
    ax.text(0,2.64,"Individual prediction agreement vs end-to-end error",fontsize=11.0,fontweight="bold",va="top")
    ax.text(.48,2.22,"Fixed prediction MAD",ha="right",fontsize=9.2,fontweight="bold",color=COLORS["pcornet"])
    ax.text(.96,2.22,"End-to-end Brier Δ",ha="right",fontsize=9.2,fontweight="bold",color=COLORS["omop"])
    mad=[e["models"][m]["fixed_probability_mad"] for m in models]; bd=[e["models"][m]["end_omop_brier"]-e["models"][m]["end_pcornet_brier"] for m in models]
    for yi,label,mv,bv in zip(ym,mlabs,mad,bd):
        ax.text(0,yi,label,va="center",fontsize=10.0)
        ax.text(.48,yi,"<0.001" if mv<.001 else f"{mv:.3f}",ha="right",va="center",fontsize=10.0,fontweight="bold" if mv<.001 else "normal",color=COLORS["pcornet"])
        ax.text(.96,yi,f"{bv:+.2f}",ha="right",va="center",fontsize=10.0,color=COLORS["omop"])
        ax.plot([.03,.96],[yi-.30,yi-.30],color="#EEEEEE",lw=.8)
    return fig

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--data",default="study_definitions/artifacts/publication_figure_data_v1.json"); ap.add_argument("--outdir",required=True); ns=ap.parse_args()
    style(); data=json.loads(Path(ns.data).read_text()); out=Path(ns.outdir); out.mkdir(parents=True,exist_ok=True)
    figs={"Figure1_reproducibility_breakpoint":figure1,"Figure2_phenotype_mechanism":figure2,"Figure3_outcome_estimands":figure3,"Figure4_model_reproducibility":figure4}
    for name,fn in figs.items():
        fig=fn(data); fig.savefig(out/f"{name}.png",dpi=200,bbox_inches="tight"); fig.savefig(out/f"{name}.pdf",bbox_inches="tight"); plt.close(fig)
if __name__=="__main__": main()