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
        "font.size":9.5, "axes.titlesize":10.5, "axes.labelsize":9.5,
        "xtick.labelsize":8.8, "ytick.labelsize":8.8, "legend.fontsize":8.8,
        "axes.linewidth":0.7, "lines.linewidth":1.0, "pdf.fonttype":42, "ps.fonttype":42,
        "figure.facecolor":"white", "axes.facecolor":"white"
    })

def clean(ax):
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False); ax.grid(False)

def panel(ax, letter, x=-0.11, y=1.06):
    ax.text(x,y,letter,transform=ax.transAxes,fontweight="bold",fontsize=12,va="top")

def box(ax, xy, w, h, text, face="white", fs=9.3, bold=False, edge="#555555", radius=.015):
    x,y=xy
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle=f"round,pad=0.004,rounding_size={radius}",facecolor=face,edgecolor=edge,linewidth=.8))
    ax.text(x+w/2,y+h/2,text,ha="center",va="center",fontsize=fs,fontweight="bold" if bold else "normal",linespacing=1.12)

def arrow(ax,a,b,color="#666666"):
    ax.add_patch(FancyArrowPatch(a,b,arrowstyle="-|>",mutation_scale=10,linewidth=.9,color=color,shrinkA=0,shrinkB=0))

def figure1(data):
    c,d=data["stage_c"],data["stage_d"]
    fig=plt.figure(figsize=(10.5,5.7))
    gs=fig.add_gridspec(2,1,height_ratios=[.92,1.08],left=.045,right=.985,top=.95,bottom=.07,hspace=.24)
    ax=fig.add_subplot(gs[0]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off")
    panel(ax,"a",x=-.025,y=1.01)
    ax.text(.025,.99,"Reproducibility breaks at cohort selection, not downstream representation",fontsize=12.5,fontweight="bold",va="top")
    xs=[.03,.29,.54,.79]; ws=[.19,.19,.19,.18]
    texts=[
        "Mapped semantics\nexact within locked\nmapped denominators",
        f"Independent D0 cohort\n{c['primary']['D0']['pcornet']:,} vs {c['primary']['D0']['omop']:,}\nJaccard {c['primary']['D0']['jaccard']:.3f}",
        "Mechanism localized\nmissing DX_DATE\nsource fallback vs ETL exclusion",
        "Harmonized eligibility\nD0/D1/D3 Jaccard 1.000\nindex dates 100% exact"
    ]
    fills=[COLORS["blue_fill"],COLORS["orange_fill"],COLORS["orange_fill"],COLORS["green_fill"]]
    for i,(x,w,t,f) in enumerate(zip(xs,ws,texts,fills)):
        box(ax,(x,.32),w,.39,t,face=f,fs=9.2,bold=i in {0,1,3})
        if i<3: arrow(ax,(x+w,.515),(xs[i+1],.515),COLORS["mid"])
    ax.text(.385,.16,"BREAKPOINT",ha="center",fontsize=8.6,fontweight="bold",color=COLORS["omop"])
    ax.plot([.385,.385],[.20,.29],color=COLORS["omop"],lw=2.3,solid_capstyle="round")

    ax=fig.add_subplot(gs[1]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off")
    panel(ax,"b",x=-.025,y=1.02)
    ax.text(.025,.98,"The same transformed data answer two different reproducibility questions",fontsize=11.5,fontweight="bold",va="top")
    ax.text(.05,.81,"ESTIMAND",fontsize=8.4,fontweight="bold",color=COLORS["mid"])
    ax.text(.34,.81,"OBSERVED RESULT",fontsize=8.4,fontweight="bold",color=COLORS["mid"])
    ax.text(.74,.81,"INTERPRETATION",fontsize=8.4,fontweight="bold",color=COLORS["mid"])
    for y0,fc in [(.45,COLORS["blue_fill"]),(.07,COLORS["orange_fill"])]:
        ax.add_patch(FancyBboxPatch((.025,y0),.95,.28,boxstyle="round,pad=.005,rounding_size=.012",facecolor=fc,edgecolor="none"))
    ax.text(.05,.59,"FIXED patient + index\ncommon observability",fontsize=10,fontweight="bold",va="center",color=COLORS["pcornet"])
    ax.text(.34,.59,f"90-day risk  {d['fixed']['90_day']['pcornet_risk_percent']:.1f}% = {d['fixed']['90_day']['omop_risk_percent']:.1f}%\n1,132/1,132 first-event dates exact",fontsize=10.5,fontweight="bold",va="center")
    ax.text(.74,.59,"Representation preserved\nfor the same study anchors",fontsize=10,fontweight="bold",va="center",color=COLORS["green"])
    ax.text(.05,.21,"INDEPENDENT end-to-end\nstudy in each CDM",fontsize=10,fontweight="bold",va="center",color=COLORS["omop"])
    e=d["end_to_end"]["90_day"]
    ax.text(.34,.21,f"90-day risk  {e['pcornet_risk_percent']:.1f}% → {e['omop_risk_percent']:.1f}%\nΔ +{e['risk_difference_pp']:.2f} pp   RR {e['rr']:.2f}",fontsize=10.5,fontweight="bold",va="center")
    ax.text(.74,.21,f"Population changed\n{e['pcornet_eligible']:,} vs {e['omop_eligible']:,} eligible",fontsize=10,fontweight="bold",va="center",color=COLORS["omop"])
    return fig

def figure2(data):
    p,h=data["stage_c"]["primary"],data["stage_c"]["harmonized_dxdate"]
    ph=["D0","D1","D3"]; y=np.arange(3)[::-1]
    fig=plt.figure(figsize=(10.5,6.2))
    gs=fig.add_gridspec(2,2,width_ratios=[1.05,1.25],height_ratios=[.78,1.22],left=.09,right=.985,top=.94,bottom=.10,hspace=.55,wspace=.30)
    ax=fig.add_subplot(gs[:,0])
    for yi,k in zip(y,ph):
        a,b=p[k]["pcornet"],p[k]["omop"]
        ax.plot([b,a],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=78,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.5,zorder=3)
        ax.scatter(b,yi,s=70,color=COLORS["omop"],zorder=3)
        ax.text(a,yi+.11,f"{a:,}",ha="center",va="bottom",fontsize=9.2)
        ax.text(b,yi-.11,f"{b:,}",ha="center",va="top",fontsize=9.2)
    ax.set(yticks=y,yticklabels=ph,xlabel="Patients"); ax.set_xlim(4800,10350)
    ax.set_title("Source-faithful phenotype size",loc="left",fontweight="bold"); clean(ax); panel(ax,"a")
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["pcornet"],ls="None",label="PCORnet"),plt.Line2D([],[],marker="o",color=COLORS["omop"],ls="None",label="OMOP")],loc="upper left",frameon=False,ncol=2)

    ax=fig.add_subplot(gs[0,1])
    for yi,k in zip(y,ph):
        a,b=p[k]["jaccard"],h[k]["jaccard"]
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=60,facecolor="white",edgecolor=COLORS["dark"],linewidth=1.2,zorder=3)
        ax.scatter(b,yi,s=60,color=COLORS["green"],zorder=3)
        ax.text(a+.004,yi-.16,f"{a:.3f}",ha="left",va="top",fontsize=8.7)
        ax.text(b-.006,yi-.16,"1.000",ha="right",va="top",fontsize=8.7,fontweight="bold",color=COLORS["green"])
    ax.set(yticks=y,yticklabels=ph,xlabel="Patient Jaccard"); ax.set_xlim(.57,1.03)
    ax.set_title("One eligibility rule restores exact membership",loc="left",fontweight="bold",pad=10); clean(ax); panel(ax,"b")
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["dark"],ls="None",label="Source-faithful"),plt.Line2D([],[],marker="o",color=COLORS["green"],ls="None",label="Symmetric nonmissing DX_DATE")],loc="upper center",bbox_to_anchor=(.50,-.27),frameon=False,ncol=2,columnspacing=1.0,handletextpad=.4)

    ax=fig.add_subplot(gs[1,1]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis("off"); panel(ax,"c")
    ax.text(0,1.03,"Mechanism localized by lineage audit",fontsize=10.7,fontweight="bold",va="bottom")
    box(ax,(.19,.78),.62,.15,"Selected stroke diagnosis: DX_DATE missing",face=COLORS["gray_fill"],fs=9.2,bold=True)
    arrow(ax,(.50,.78),(.50,.70),COLORS["mid"])
    ax.plot([.24,.76],[.70,.70],color=COLORS["mid"],lw=.9)
    arrow(ax,(.24,.70),(.24,.61),COLORS["pcornet"]); arrow(ax,(.76,.70),(.76,.61),COLORS["omop"])
    box(ax,(.04,.40),.40,.20,"PCORnet phenotype\nencounter-date fallback\nepisode remains eligible",face=COLORS["blue_fill"],fs=9,bold=True,edge=COLORS["pcornet"])
    box(ax,(.56,.40),.40,.20,"Frozen ETL\nno diagnosis event materialized\nselected episode absent",face=COLORS["orange_fill"],fs=9,bold=True,edge=COLORS["omop"])
    arrow(ax,(.24,.40),(.37,.28),COLORS["green"]); arrow(ax,(.76,.40),(.63,.28),COLORS["green"])
    box(ax,(.13,.06),.74,.21,"Require nonmissing DX_DATE in both representations\nD0/D1/D3: Jaccard 1.000 · index dates 100% exact",face=COLORS["green_fill"],fs=9.5,bold=True,edge=COLORS["green"])
    return fig

def figure3(data):
    d=data["stage_d"]; labels=["30 days","90 days"]; y=np.array([1,0])
    fig=plt.figure(figsize=(10.5,6.0)); gs=fig.add_gridspec(2,2,height_ratios=[1,1.05],left=.09,right=.985,top=.94,bottom=.11,hspace=.45,wspace=.30)
    ax=fig.add_subplot(gs[0,0])
    for yi,key in zip(y,["30_day","90_day"]):
        r=d["fixed"][key]; x=r["pcornet_risk_percent"]
        ax.scatter(x,yi,s=82,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.6,zorder=3)
        ax.scatter(x,yi,s=38,color=COLORS["omop"],zorder=4)
        ax.text(x+.45,yi,f"{x:.1f}% = {r['omop_risk_percent']:.1f}%",va="center",fontsize=9.8,fontweight="bold")
    ax.set(yticks=y,yticklabels=labels,xlabel="Acute-care risk (%)"); ax.set_xlim(14,33); clean(ax); panel(ax,"a")
    ax.set_title("Same patient + index: outcome representation is exact",loc="left",fontweight="bold")
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["pcornet"],ls="None",label="PCORnet"),plt.Line2D([],[],marker="o",color=COLORS["omop"],ls="None",label="OMOP")],loc="center right",frameon=False,ncol=1)

    ax=fig.add_subplot(gs[0,1])
    for yi,key in zip(y,["30_day","90_day"]):
        r=d["end_to_end"][key]; a,b=r["pcornet_risk_percent"],r["omop_risk_percent"]
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.2)
        ax.scatter(a,yi,s=75,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.5,zorder=3)
        ax.scatter(b,yi,s=65,color=COLORS["omop"],zorder=3)
        tx=max(a,b)+.55
        if key == "30_day":
            ay1, ay2 = yi-.08, yi-.22
        else:
            ay1, ay2 = yi+.10, yi-.08
        ax.text(tx,ay1,f"Δ +{r['risk_difference_pp']:.2f} pp · RR {r['rr']:.2f}",ha="left",va="center",fontsize=9,fontweight="bold")
        ax.text(tx,ay2,f"eligible n: {r['pcornet_eligible']:,} vs {r['omop_eligible']:,}",ha="left",va="center",fontsize=8.5,color=COLORS["mid"])
    ax.set(yticks=y,yticklabels=labels,xlabel="Acute-care risk (%)"); ax.set_xlim(14,35); ax.set_ylim(-.18,1.15); clean(ax); panel(ax,"b")
    ax.set_title("Independent study: risk changes as the population changes",loc="left",fontweight="bold")

    ax=fig.add_subplot(gs[1,:]); labs=["Fixed 30 d","Fixed 90 d","End-to-end 30 d","End-to-end 90 d"]; yy=np.arange(4)[::-1]
    vals=[d["fixed"]["30_day"]["risk_difference_pp"],d["fixed"]["90_day"]["risk_difference_pp"],d["end_to_end"]["30_day"]["risk_difference_pp"],d["end_to_end"]["90_day"]["risk_difference_pp"]]
    m=d["equivalence_margins"]["risk_difference_pp"]
    ax.axvspan(-m,m,color=COLORS["green_fill"],zorder=0); ax.axvline(0,color=COLORS["dark"],lw=.8)
    ax.scatter(vals,yy,s=70,c=[COLORS["pcornet"],COLORS["pcornet"],COLORS["omop"],COLORS["omop"]],zorder=3)
    for x,yi in zip(vals,yy): ax.text(x+.06,yi,f"{x:+.2f}",va="center",fontsize=9.2,fontweight="bold")
    ax.set(yticks=yy,yticklabels=labs,xlabel="OMOP − PCORnet risk difference (percentage points)"); ax.set_xlim(-.7,2.25); clean(ax); panel(ax,"c",x=-.055)
    ax.set_title("Only the end-to-end estimand exceeds the prespecified reproducibility tolerance (±0.5 pp)",loc="left",fontweight="bold")
    return fig

def figure4(data):
    e=data["stage_e"]
    features=["Age","Female","Index length of stay","Prior acute-care encounters","Prior all encounters","Prior ischemic stroke"]
    y=np.arange(6)[::-1]; models=list(e["models"]); mlabs=["Logistic","Ridge logistic","Gradient boosting"]; ym=np.arange(3)[::-1]
    fig=plt.figure(figsize=(10.5,6.2)); gs=fig.add_gridspec(2,2,width_ratios=[1.08,1],left=.18,right=.985,top=.94,bottom=.11,hspace=.42,wspace=.30)
    ax=fig.add_subplot(gs[:,0]); fs=[abs(e["fixed_feature_smd"][x]) for x in features]; es=[abs(e["end_to_end_feature_smd"][x]) for x in features]
    ax.axvline(.10,color=COLORS["mid"],ls="--",lw=.9)
    for yi,a,b in zip(y,fs,es):
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2.0)
        ax.scatter(a,yi,s=65,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.4,zorder=3)
        ax.scatter(b,yi,s=60,color=COLORS["omop"],zorder=3)
        if b>=.10: ax.text(b+.006,yi,f"{b:.2f}",va="center",fontsize=8.9,fontweight="bold")
    ax.set(yticks=y,yticklabels=features,xlabel="Absolute standardized mean difference"); ax.set_xlim(-.005,.18); clean(ax); panel(ax,"a",x=-.27)
    ax.set_title("Case mix shifts only when cohorts are built independently",loc="left",fontweight="bold")
    ax.text(.103,5.25,"0.10 reference",fontsize=8.3,color=COLORS["mid"])
    ax.legend(handles=[plt.Line2D([],[],marker="o",mfc="white",mec=COLORS["pcornet"],ls="None",label="Fixed cohort"),plt.Line2D([],[],marker="o",color=COLORS["omop"],ls="None",label="End-to-end")],loc="lower right",frameon=False)

    ax=fig.add_subplot(gs[0,1]); fa=[e["models"][m]["fixed_auroc_difference"] for m in models]; ea=[e["models"][m]["end_auroc_difference"] for m in models]
    ax.axvline(0,color=COLORS["dark"],lw=.8)
    for yi,a,b in zip(ym,fa,ea):
        ax.plot([a,b],[yi,yi],color=COLORS["light"],lw=2)
        ax.scatter(a,yi,s=60,facecolor="white",edgecolor=COLORS["pcornet"],linewidth=1.4,zorder=3)
        ax.scatter(b,yi,s=55,color=COLORS["omop"],zorder=3)
        ax.text(b-.001,yi+.11,f"{b:.2f}",ha="right",fontsize=8.8,fontweight="bold")
    ax.set(yticks=ym,yticklabels=mlabs,xlabel="AUROC difference (OMOP − PCORnet)"); ax.set_xlim(-.052,.004); clean(ax); panel(ax,"b",x=-.20)
    ax.set_title("Discrimination is stable fixed, shifted end-to-end",loc="left",fontweight="bold")

    ax=fig.add_subplot(gs[1,1]); ax.set(xlim=(0,1),ylim=(-.6,2.6)); ax.axis("off"); panel(ax,"c",x=-.20)
    ax.text(0,2.55,"Individual prediction agreement vs end-to-end error",fontsize=10.4,fontweight="bold",va="top")
    ax.text(.48,2.12,"Fixed prediction MAD",ha="right",fontsize=8.8,fontweight="bold",color=COLORS["pcornet"])
    ax.text(.96,2.12,"End-to-end Brier Δ",ha="right",fontsize=8.8,fontweight="bold",color=COLORS["omop"])
    mad=[e["models"][m]["fixed_probability_mad"] for m in models]; bd=[e["models"][m]["end_omop_brier"]-e["models"][m]["end_pcornet_brier"] for m in models]
    for yi,label,mv,bv in zip(ym,mlabs,mad,bd):
        ax.text(0,yi,label,va="center",fontsize=9.1)
        ax.text(.48,yi,"<0.001" if mv<.001 else f"{mv:.3f}",ha="right",va="center",fontsize=9.5,fontweight="bold" if mv<.001 else "normal",color=COLORS["pcornet"])
        ax.text(.96,yi,f"{bv:+.2f}",ha="right",va="center",fontsize=9.5,color=COLORS["omop"])
        ax.plot([.03,.96],[yi-.31,yi-.31],color="#EEEEEE",lw=.8)
    ax.text(0,-.47,"Fixed logistic predictions are nearly identical; end-to-end differences reflect different empirical populations.",fontsize=8.6,color=COLORS["mid"])
    return fig

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--data",default="study_definitions/artifacts/publication_figure_data_v1.json"); ap.add_argument("--outdir",required=True); ns=ap.parse_args()
    style(); data=json.loads(Path(ns.data).read_text()); out=Path(ns.outdir); out.mkdir(parents=True,exist_ok=True)
    figs={"Figure1_reproducibility_breakpoint":figure1,"Figure2_phenotype_mechanism":figure2,"Figure3_outcome_estimands":figure3,"Figure4_model_reproducibility":figure4}
    for name,fn in figs.items():
        fig=fn(data); fig.savefig(out/f"{name}.png",dpi=200,bbox_inches="tight"); fig.savefig(out/f"{name}.pdf",bbox_inches="tight"); plt.close(fig)
if __name__=="__main__": main()
