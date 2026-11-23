"""Reader-facing Figures 1 and 4 from the versioned public publication aggregates.

These functions are imported by the canonical publication figure runner in the
repository. They only use the existing stage C/D/E publication JSON, never
patient-level data. All numerical quantities are computed from input values.
"""
from __future__ import annotations
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

BLUE='#0072B2'; ORANGE='#D55E00'; GREEN='#009E73'; DARK='#242424'; GRAY='#7A7A7A'; LIGHT='#D5D5D5'

def _panel(ax, letter, title):
    ax.set_title(title, loc='left', fontsize=11, weight='bold', pad=12)
    ax.text(-.06,1.1,letter,transform=ax.transAxes,fontsize=12,weight='bold',va='bottom')

def _card(ax, pos, size, title, line, face):
    x,y=pos;w,h=size
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.008,rounding_size=0.018',facecolor=face,edgecolor='#B7B7B7',lw=.9,transform=ax.transAxes))
    ax.text(x+w/2,y+h*.66,title,ha='center',va='center',weight='bold',fontsize=9.4,transform=ax.transAxes)
    ax.text(x+w/2,y+h*.31,line,ha='center',va='center',fontsize=8.8,linespacing=1.25,transform=ax.transAxes)

def figure1_reader(data):
    """Show documented exclusions and distinct fixed vs end-to-end results."""
    c=data['stage_c']['primary']['D0'];d=data['stage_d']
    f=d['fixed']['90_day'];e=d['end_to_end']['90_day']
    assert c['pcornet']==c['shared']+c['source_only'] and c['omop']==c['shared']
    fig=plt.figure(figsize=(11.5,5.6)); gs=fig.add_gridspec(2,1,height_ratios=[.94,1.06],left=.055,right=.965,top=.91,bottom=.09,hspace=.41)
    a=fig.add_subplot(gs[0]);a.axis('off')
    _panel(a,'a','The exclusion rule was documented, but its effect on study results required evaluation')
    xx=[.015,.263,.511,.759];w=.222;h=.55
    cards=[('Mapping and routing','Expected destinations and\nretained values agreed','#EAF4FA'),
           ('Transformation policy','Diagnoses without a recorded\ndate were excluded','#FBEDE6'),
           ('Cohort retention',f"{c['omop']:,} of {c['pcornet']:,} source\npatients retained ({100*c['omop']/c['pcornet']:.1f}%)",'#FBEDE6'),
           ('Study impact','Membership and outcomes tested\nunder two alternative date rules','#EAF6F2')]
    for j,(title,desc,col) in enumerate(cards):
        _card(a,(xx[j],.21),(w,h),title,desc,col)
        if j<3:a.annotate('',xy=(xx[j+1]-.002,.485),xytext=(xx[j]+w+.006,.485),xycoords='axes fraction',arrowprops={'arrowstyle':'->','color':GRAY,'lw':1.4})
    b=fig.add_subplot(gs[1]);b.axis('off')
    _panel(b,'b','Outcome agreement depends on whether the same patients are compared')
    for y,face in [(.50,'#EAF4FA'),(.075,'#FBEDE6')]:
        b.add_patch(FancyBboxPatch((.012,y),.975,.34,boxstyle='round,pad=.005,rounding_size=.014',facecolor=face,edgecolor='none',transform=b.transAxes))
    b.text(.043,.72,'Same patients, index dates\nand follow-up',fontsize=10,weight='bold',color=BLUE,transform=b.transAxes,va='center')
    b.text(.39,.72,f"90-day risk: {f['pcornet_risk_percent']:.1f}% vs {f['omop_risk_percent']:.1f}%\n{f['pcornet_events']:,} events in each",fontsize=10.8,weight='bold',transform=b.transAxes,va='center')
    b.text(.77,.72,'Exact agreement\nfor matched patients',fontsize=10,color=GREEN,weight='bold',transform=b.transAxes,va='center')
    b.text(.043,.25,'Source-selected vs\nretained OMOP population',fontsize=10,weight='bold',color=ORANGE,transform=b.transAxes,va='center')
    b.text(.39,.25,f"90-day risk: {e['pcornet_risk_percent']:.1f}% vs {e['omop_risk_percent']:.1f}%\nDifference: +{e['risk_difference_pp']:.2f} percentage points",fontsize=10.8,weight='bold',transform=b.transAxes,va='center')
    b.text(.77,.25,f"Different populations\n{e['pcornet_eligible']:,} vs {e['omop_eligible']:,} eligible",fontsize=10,color=ORANGE,weight='bold',transform=b.transAxes,va='center')
    return fig

def figure4_reader(data):
    """Compare features and models without a misplaced Brier note."""
    e=data['stage_e']; feat=['Age','Female','Index length of stay','Prior acute-care encounters','Prior all encounters','Prior ischemic stroke']
    models=list(e['models'].keys()); mlab=['Logistic','Ridge logistic','Gradient boosting']; colors={'full':BLUE,'shared':BLUE}
    fig=plt.figure(figsize=(11.2,6.25));gs=fig.add_gridspec(2,2,width_ratios=[1.06,1],height_ratios=[1.02,.92],left=.20,right=.975,top=.91,bottom=.11,wspace=.39,hspace=.53)
    a=fig.add_subplot(gs[:,0]);y=np.arange(len(feat))[::-1]
    vals=[abs(e['end_to_end_feature_smd'][x]) for x in feat];fixed=[abs(e['fixed_feature_smd'][x]) for x in feat]
    for yi,aa,bb in zip(y,fixed,vals):
        a.plot([aa,bb],[yi,yi],lw=2,color=LIGHT,zorder=1);a.scatter(aa,yi,s=65,facecolors='white',edgecolors=BLUE,linewidths=1.4,zorder=3);a.scatter(bb,yi,s=66,color=ORANGE,zorder=3)
    a.axvline(.10,color=GRAY,lw=1,linestyle='--');
    a.set_yticks(y,feat);a.set_ylim(-.4,5.55);a.set_xlim(-.008,.183);a.set_xlabel('Absolute standardized mean difference')
    a.spines[['top','right']].set_visible(False)
    _panel(a,'a','Cohort retention changes\npredictor distributions')
    ha=[plt.Line2D([],[],color=BLUE,marker='o',mfc='white',ls='None',label='Matched'),plt.Line2D([],[],color=ORANGE,marker='o',ls='None',label='End-to-end')]
    a.legend(handles=ha,frameon=False,ncol=2,loc='upper right',fontsize=8.7)
    b=fig.add_subplot(gs[0,1]); yb=np.arange(3)[::-1]
    for yi,k in zip(yb,models):
        v=e['models'][k];aa=v['fixed_pcornet_auroc'];bb=v['end_pcornet_auroc'];b.plot([aa,bb],[yi,yi],lw=2,color=LIGHT);b.scatter(aa,yi,s=68,facecolors='white',edgecolors=BLUE,linewidths=1.6,zorder=3);b.scatter(bb,yi,s=70,color=BLUE,zorder=4)
        b.annotate(f'{aa:.3f}',(aa,yi),xytext=(0,-18),textcoords='offset points',ha='center',fontsize=8.5)
        b.annotate(f'{bb:.3f}',(bb,yi),xytext=(0,10),textcoords='offset points',ha='center',fontsize=8.5,weight='bold')
    b.set_yticks(yb,mlab);b.set_ylim(-1.12,2.55);b.set_xlim(.575,.651);b.set_xlabel('AUROC');b.spines[['top','right']].set_visible(False)
    _panel(b,'b','PCORnet AUROC by population')
    hb=[plt.Line2D([],[],color=BLUE,marker='o',ls='None',label='Full PCORnet'),plt.Line2D([],[],marker='o',color=BLUE,mfc='white',ls='None',label='Shared PCORnet')]
    b.legend(handles=hb,frameon=False,loc='lower center',bbox_to_anchor=(.5,.008),fontsize=8.4,ncol=2)
    c=fig.add_subplot(gs[1,1]);c.axis('off');_panel(c,'c','Prediction agreement and error')
    c.text(.025,.83,'Model',weight='bold',fontsize=8.7,transform=c.transAxes)
    c.text(.49,.83,'Fixed patient\nprediction MAD',color=BLUE,weight='bold',fontsize=8.5,ha='center',transform=c.transAxes)
    c.text(.89,.83,'End-to-end\nBrier difference',color=ORANGE,weight='bold',fontsize=8.5,ha='center',transform=c.transAxes)
    for yi,k,l in zip((.63,.43,.23),models,mlab):
        v=e['models'][k];bd=v['end_omop_brier']-v['end_pcornet_brier'];mad=v['fixed_probability_mad']
        c.text(.025,yi,l,transform=c.transAxes,fontsize=8.6,va='center')
        c.text(.49,yi,'<0.001' if mad<.001 else f'{mad:.3f}',transform=c.transAxes,fontsize=9,color=BLUE,ha='center',va='center')
        c.text(.89,yi,f'{bd:+.3f}',transform=c.transAxes,fontsize=9,color=ORANGE,ha='center',va='center')
        c.plot([.025,.985],[yi-.10,yi-.10],transform=c.transAxes,lw=.6,color='#E4E4E4')
    c.text(.025,.055,'Brier difference = OMOP minus PCORnet.\nEnd-to-end comparison uses different held-out populations.',transform=c.transAxes,fontsize=8.3,color=GRAY,va='top')
    return fig
