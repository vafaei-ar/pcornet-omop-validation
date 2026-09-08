from __future__ import annotations

"""Generate canonical publication figures from disclosure-reviewed aggregate results.

The canonical plotting pipeline reads only committed aggregate artifacts; no patient-
level data are required. Version 2 extends the original frozen publication artifact
with post-freeze mechanistic sensitivity results while retaining the frozen ETL SHA.
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

DEFAULT_DATA = Path("study_definitions/artifacts/publication_figure_data_v2.json")

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
            "font.sans-serif": [font_name, "Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
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
    ax.text(x, y, letter, transform=ax.transAxes, fontweight="bold", fontsize=12.8, va="top")


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
            (x, y), w, h,
            boxstyle=f"round,pad=0.004,rounding_size={radius}",
            facecolor=face, edgecolor=edge, linewidth=0.85,
        )
    )
    ax.text(
        x + w / 2, y + h / 2, text,
        ha="center", va="center", fontsize=fs,
        fontweight="bold" if bold else "normal", linespacing=1.10,
    )


def arrow(ax: plt.Axes, a: tuple[float, float], b: tuple[float, float], color: str = "#666666") -> None:
    ax.add_patch(
        FancyArrowPatch(
            a, b, arrowstyle="-|>", mutation_scale=11, linewidth=0.95,
            color=color, shrinkA=0, shrinkB=0,
        )
    )


def validate_data(data: dict) -> None:
    version = data.get("version")
    if version not in {"publication_figure_data_v1", "publication_figure_data_v2"}:
        raise ValueError("Unexpected publication figure-data version")

    for phenotype in ("D0", "D1", "D3"):
        harmonized = data["stage_c"]["harmonized_dxdate"][phenotype]
        if not (
            harmonized["pcornet"] == harmonized["omop"] == harmonized["shared"]
            and harmonized["source_only"] == 0
            and harmonized["omop_only"] == 0
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
        numeric["direct_exact"] + numeric["explained_vital_differences"] != numeric["comparable"]
        or numeric["unexplained"] != 0
    ):
        raise ValueError("Stage B numeric reconciliation invariant failed")

    if version == "publication_figure_data_v2":
        for phenotype in ("D0", "D1", "D3"):
            fallback = data["stage_c"]["encounter_date_fallback"][phenotype]
            if not (
                fallback["pcornet"] == fallback["target"] == fallback["shared"]
                and fallback["source_only"] == 0
                and fallback["target_only"] == 0
                and fallback["jaccard"] == 1.0
                and fallback["exact_index_percent"] == 100.0
            ):
                raise ValueError(f"Fallback Stage C invariant failed for {phenotype}")
        for window in ("30_day", "90_day"):
            fallback = data["stage_d"]["fallback_end_to_end"][window]
            if not (
                fallback["risk_difference_pp"] == 0
                and fallback["rr"] == 1
                and fallback["pcornet_eligible"] == fallback["target_eligible"] == fallback["label_agreement"]
            ):
                raise ValueError(f"Fallback Stage D invariant failed for {window}")


def load_data(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_data(data)
    return data


def figure1(data: dict) -> plt.Figure:
    stage_c = data["stage_c"]
    stage_d = data["stage_d"]
    fig = plt.figure(figsize=(10.5, 5.15))
    gs = fig.add_gridspec(2, 1, height_ratios=[0.88, 1.12], left=0.045, right=0.985, top=0.95, bottom=0.07, hspace=0.10)

    ax = fig.add_subplot(gs[0])
    ax.set(xlim=(0, 1), ylim=(0, 1)); ax.axis("off")
    panel(ax, "a", x=-0.025, y=1.01)
    ax.text(0.025, 0.99, "Reproducibility breaks at cohort selection, not downstream representation", fontsize=13.0, fontweight="bold", va="top")
    xs = [0.03, 0.29, 0.54, 0.78]
    widths = [0.19, 0.19, 0.20, 0.19]
    texts = [
        "Mapped semantics\nexact within locked\nmapped denominators",
        f"Independent base cohort (D0)\n{stage_c['primary']['D0']['pcornet']:,} vs {stage_c['primary']['D0']['omop']:,}\nJaccard {stage_c['primary']['D0']['jaccard']:.3f}",
        "Mechanism localized\nmissing diagnosis date\nfallback vs exclusion",
        "Same diagnosis-date rule\n3 p²È="25ä°±…‰•±Ìõµ½‘•±}±…‰•±Ì¤ì…à¹Í•Ñ}á±¥´ À¸äÐ°€Ä¸ÀÀÈ¤(€€€…à¹Í•Ñ}á±…‰•° ‰A•…ÉÍ½¸½ÉÉ•±…Ñ¥½¸ˆ¤ì…à¹Í•Ñ}Ñ¥Ñ±” ‰¥á•ÁÉ•‘¥Ñ¥½¸…É••µ•¹Ðˆ°™½¹ÑÝ•¥¡Ðô‰‰½±ˆ°Á…ôÄÔ¤ì±•…¸¡…à¤ìÁ…¹•°¡…à°€‰ˆˆ¤(€€€™½ÈÙ…±Õ”°å¤¥¸é¥À¡Ù…±Õ•Ì°åä¤è(€€€€€€€å}Ñ•áÐ°Ù„€ô€ Ä¸äÈ°€‰Ñ½Àˆ¤¥˜å¤€ø€Ä¸Ô…¹Ù…±Õ”€ø€À¸äää•±Í”€¡å¤€¬€À¸ÄÈ°€‰‰…Í•±¥¹”ˆ¤(€€€€€€€…à¹Ñ•áÐ¡Ù…±Õ”€´€À¸ÀÀÄ°å}Ñ•áÐ°€ˆøÀ¸äääˆ¥˜Ù…±Õ”€ø€À¸äää•±Í”˜‰íÙ…±Õ”è¸Í™ôˆ°¡„ô‰É¥¡Ðˆ°Ù„õÙ„°™½¹ÑÍ¥é”ôà¸à¤(€€€…à€ô…áÍlÉt(€€€±…‰•±Ì€ôl‰AÉ¥µ…ÉäÉ•ÕÉÉ•¹Ñq¹ÍÑÉ½­”µ½‘”•¹‘Á½¥¹Ðˆ°€‰A½ÍÐµ½ÕÑ½µ”ÁÉ¥¹¥Á…°µ‘¥…¹½Í¥Íq¹Í•¹Í¥Ñ¥Ù¥Ñä‰t(€€€åä€ô¹À¹…ÉÉ…ä¡lÄ°€Át¤ìÁ½É¹•Ñ}•Ù•¹ÑÌ€ômÉ•ÕÉÉ•¹Ñl‰ÁÉ¥µ…Éå}Á½É¹•Ñ}•Ù•¹ÑÌ‰t°É•ÕÉÉ•¹Ñl‰Á‘á}ÁÉ¥µ…Éå}Í•¹Í¥Ñ¥Ù¥Ñå}Á½É¹•Ñ}•Ù•¹ÑÌ‰utì½µ½Á}•Ù•¹ÑÌ€ômÉ•ÕÉÉ•¹Ñl‰ÁÉ¥µ…Éå}½µ½Á}•Ù•¹ÑÌ‰t°É•ÕÉÉ•¹Ñl‰Á‘á}ÁÉ¥µ…Éå}Í•¹Í¥Ñ¥Ù¥Ñå}½µ½Á}•Ù•¹ÑÌ‰ut(€€€™½Èå¤°Á½É¹•Ñ}Ù…±Õ”°½µ½Á}Ù…±Õ”¥¸é¥À¡åä°Á½É¹•Ñ}•Ù•¹ÑÌ°½µ½Á}•Ù•¹ÑÌ¤è(€€€€€€€…à¹Í…ÑÑ•È¡Á½É¹•Ñ}Ù…±Õ”°å¤€¬€À¸ÀÔ°ÌôÔÔ°™…•½±½Èô‰Ý¡¥Ñ”ˆ°•‘•½±½Èõ=1=IMl‰Á½É¹•Ð‰t°±¥¹•Ý¥‘Ñ ôÄ¸Ð¤(€€€€€€€…à¹Í…ÑÑ•È¡½µ½Á}Ù…±Õ”°å¤€´€À¸ÀÔ°ÌôÔÀ°½±½Èõ=1=IMl‰½µ½À‰t¤ì…à¹Ñ•áÐ¡Á½É¹•Ñ}Ù…±Õ”€¬€È°å¤€¬€À¸ÄÀ°ÍÑÈ¡Á½É¹•Ñ}Ù…±Õ”¤°™½¹ÑÍ¥é”ôà¸à¤(€€€€€€€½É…¹•}ä°½É…¹•}à°½É…¹•}Ù„€ô€ ´À¸ÀÔ°½µ½Á}Ù…±Õ”€¬€Ø°€‰•¹Ñ•Èˆ¤¥˜å¤€ôô€À…¹½µ½Á}Ù…±Õ”€ôô€ÄÜÀ•±Í”€¡å¤€´€À¸ÄØ°½µ½Á}Ù…±Õ”€¬€È°€‰‰…Í•±¥¹”ˆ¤(€€€€€€€…à¹Ñ•áÐ¡½É…¹•}à°½É…¹•}ä°ÍÑÈ¡½µ½Á}Ù…±Õ”¤°™½¹ÑÍ¥é”ôà¸à°Ù„õ½É…¹•}Ù„¤(€€€…à¹Í•Ñ}åÑ¥­Ì¡åä°±…‰•±Ìõ±…‰•±Ì¤ì…à¹Í•Ñ}á±¥´ ÄÔÀ°€ÈàÀ¤ì…à¹Í•Ñ}å±¥´ ´À¸Äà°€Ä¸Äà¤ì…à¹Í•Ñ}á±…‰•° ‰A…Ñ¥•¹ÑÌÝ¥Ñ É•ÕÉÉ•¹Ð•Ù•¹Ðˆ¤(€€€…à¹Í•Ñ}Ñ¥Ñ±” ‰I•ÕÉÉ•¹ÐµÍÑÉ½­”Í•¹Í¥Ñ¥Ù¥Ñäˆ°™½¹ÑÝ•¥¡Ðô‰‰½±ˆ°Á…ôÄÔ¤ì±•…¸¡…à¤ìÁ…¹•°¡…à°€‰Œˆ¤(€€€…à¹±••¹¡¡…¹‘±•ÌõmÁ±Ð¹1¥¹”É¡mt°mt°µ…É­•Èô‰¼ˆ°µ™Œô‰Ý¡¥Ñ”ˆ°µ•Œõ=1=IMl‰Á½É¹•Ð‰t°±Ìô‰9½¹”ˆ°±…‰•°ô‰A=I¹•Ðˆ¤°Á±Ð¹1¥¹”É¡mt°mt°µ…É­•Èô‰¼ˆ°½±½Èõ=1=IMl‰½µ½À‰t°±Ìô‰9½¹”ˆ°±…‰•°ô‰=5=@ˆ¥t°±½Œô‰±½Ý•ÈÉ¥¡Ðˆ°™É…µ•½¸õ…±Í”°¡…¹‘±•Ñ•áÑÁ…ôÀ¸Ð¤(€€€É•ÑÕÉ¸™¥œ(()‘•˜•áÑ•¹‘•Ì¡‘…Ñ„è‘¥Ð¤€´øÁ±Ð¹¥ÕÉ”è(€€€…±¥‰É…Ñ¥½¸€ô‘…Ñ…l‰ÍÑ…•}”‰ul‰…±¥‰É…Ñ¥½¸‰tìµ½‘•±Ì€ô±¥ÍÐ¡…±¥‰É…Ñ¥½¸¤ìµ½‘•±}±…‰•±Ì€ôl‰1½¥ÍÑ¥Œˆ°€‰I¥‘”±½¥ÍÑ¥Œˆ°€‰É…‘¥•¹Ð‰½½ÍÑ¥¹œ‰tìä€ô¹À¹…É…¹” Ì¥lèè´Åt(€€€™¥œ°…áÌ€ôÁ±Ð¹ÍÕ‰Á±½ÑÌ Ä°€È°™¥Í¥é”ô ä¸Ô°€Ð¸Ø¤¤ì™¥œ¹ÍÕ‰Á±½ÑÍ}…‘©ÕÍÐ¡±•™ÐôÀ¸ÄÐ°É¥¡ÐôÀ¸äà°Ñ½ÀôÀ¸àà°‰½ÑÑ½´ôÀ¸ÈÀ°ÝÍÁ…”ôÀ¸ÐÀ¤(€€€™½ÈÁ…¹•±}¥¹‘•à°µ•ÑÉ¥Œ¥¸•¹Õµ•É…Ñ”¡l‰Í±½Á”ˆ°€‰¥¹Ñ•É•ÁÐ‰t¤è(€€€€€€€…à€ô…áÍmÁ…¹•±}¥¹‘•átìÉ•™•É•¹”€ô€Ä¥˜µ•ÑÉ¥Œ€ôô€‰Í±½Á”ˆ•±Í”€Àì…à¹…áÙ±¥¹”¡É•™•É•¹”°½±½Èõ=1=IMl‰‘…É¬‰t°±Ìôˆ´´ˆ°±ÜôÀ¸à¤(€€€€€€€™½Èå¤°µ½‘•°¥¸é¥À¡ä°µ½‘•±Ì¤è(€€€€€€€€€€€™¥á•€ô…±¥‰É…Ñ¥½¹mµ½‘•±ul‰™¥á•‰tì•¹‘}Ñ½}•¹€ô…±¥‰É…Ñ¥½¹mµ½‘•±ul‰•¹‘}Ñ½}•¹‰t(€€€€€€€€€€€™½ÈÉ•ÁÉ•Í•¹Ñ…Ñ¥½¸°½™™Í•Ð¥¸l ‰Á½É¹•Ðˆ°€À¸ÄÀ¤°€ ‰½µ½Àˆ°€´À¸ÄÀ¥tè(€€€€€€€€€€€€€€€™¥á•‘}Ù…±Õ”€ô™¥á•‘m˜‰íÉ•ÁÉ•Í•¹Ñ…Ñ¥½¹õ}íµ•ÑÉ¥ô‰tì•¹‘}Ù…±Õ”€ô•¹‘}Ñ½}•¹‘m˜‰íÉ•ÁÉ•Í•¹Ñ…Ñ¥½¹õ}íµ•ÑÉ¥ô‰t(€€€€€€€€€€€€€€€…à¹Á±½Ð¡m™¥á•‘}Ù…±Õ”°•¹‘}Ù…±Õ•t°må¤€¬½™™Í•Ð°å¤€¬½™™Í•Ñt°½±½Èõ=1=IMl‰±¥¡Ð‰t°±ÜôÈ¤(€€€€€€€€€€€€€€€…à¹Í…ÑÑ•È¡™¥á•‘}Ù…±Õ”°å¤€¬½™™Í•Ð°ÌôÔÀ°™…•½±½Èô‰Ý¡¥Ñ”ˆ¥˜É•ÁÉ•Í•¹Ñ…Ñ¥½¸€ôô€‰Á½É¹•Ðˆ•±Í”=1=IMl‰Á½É¹•Ð‰t°•‘•½±½Èõ=1=IMl‰Á½É¹•Ð‰t°±¥¹•Ý¥‘Ñ ôÄ¸È°é½É‘•ÈôÌ¤(€€€€€€€€€€€€€€€…à¹Í…ÑÑ•È¡•¹‘}Ù…±Õ”°å¤€¬½™™Í•Ð°ÌôÔÀ°™…•½±½Èô‰Ý¡¥Ñ”ˆ¥˜É•ÁÉ•Í•¹Ñ…Ñ¥½¸€ôô€‰Á½É¹•Ðˆ•±Í”=1=IMl‰½µ½À‰t°•‘•½±½Èõ=1=IMl‰½µ½À‰t°±¥¹•Ý¥‘Ñ ôÄ¸È°é½É‘•ÈôÌ¤(€€€€€€€…à¹Í•Ñ}åÑ¥­Ì¡ä°±…‰•±Ìõµ½‘•±}±…‰•±Ì¤ì…à¹Í•Ñ}á±…‰•°¡˜‰…±¥‰É…Ñ¥½¸íµ•ÑÉ¥ôˆ¤ì…à¹Í•Ñ}Ñ¥Ñ±”¡˜‰…±¥‰É…Ñ¥½¸íµ•ÑÉ¥ôˆ°™½¹ÑÝ•¥¡Ðô‰‰½±ˆ¤ì±•…¸¡…à¤ìÁ…¹•°¡…à°¡È¡½É ‰„ˆ¤€¬Á…¹•±}¥¹‘•à¤¤(€€€É•ÑÕÉ¸™¥œ(()	U%1IL€ôì(€€€5%9}95MlÁtè™¥ÕÉ”Ä°(€€€5%9}95MlÅtè™¥ÕÉ”È°(€€€5%9}95MlÉtè™¥ÕÉ”Ì°(€€€5%9}95MlÍtè™¥ÕÉ”Ð°(€€€aQ9}95MlÁtè•áÑ•¹‘•Ä°(€€€aQ9}95MlÅtè•áÑ•¹‘•È°(€€€aQ9}95MlÉtè•áÑ•¹‘•Ì°)ô(()‘•˜Í¡„ÈÔÙ}™¥±”¡Á…Ñ èA…Ñ ¤€´øÍÑÈè(€€€‘¥•ÍÐ€ô¡…Í¡±¥ˆ¹Í¡„ÈÔØ ¤(€€€Ý¥Ñ Á…Ñ ¹½Á•¸ ‰Éˆˆ¤…Ì¡…¹‘±”è(€€€€€€€™½È¡Õ¹¬¥¸¥Ñ•È¡±…µ‰‘„è¡…¹‘±”¹É•… ÄÀÈÐ€¨€ÄÀÈÐ¤°ˆˆˆ¤è(€€€€€€€€€€€‘¥•ÍÐ¹ÕÁ‘…Ñ”¡¡Õ¹¬¤(€€€É•ÑÕÉ¸‘¥•ÍÐ¹¡•á‘¥•ÍÐ ¤(()‘•˜¥Ñ}Í¡„ ¤€´øÍÑÈè(€€€ÑÉäè(€€€€€€€É•ÑÕÉ¸ÍÕ‰ÁÉ½•ÍÌ¹¡•­}½ÕÑÁÕÐ¡l‰¥Ðˆ°€‰É•ØµÁ…ÉÍ”ˆ°€‰!‰t°Ñ•áÐõQÉÕ”°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤¹ÍÑÉ¥À ¤(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€É•ÑÕÉ¸€‰Õ¹­¹½Ý¸ˆ(()‘•˜Í…Ù•}™¥ÕÉ”¡™¥œèÁ±Ð¹¥ÕÉ”°ÍÑ•´èA…Ñ °™½Éµ…ÑÌèÑÕÁ±•mÍÑÈ°€¸¸¹t°‘Á¤è¥¹Ð¤€´ø±¥ÍÑmA…Ñ¡tè(€€€ÝÉ¥ÑÑ•¸è±¥ÍÑmA…Ñ¡t€ômt(€€€™½È™µÐ¥¸™½Éµ…ÑÌè(€€€€€€€Á…Ñ €ôÍÑ•´¹Ý¥Ñ¡}ÍÕ™™¥à¡˜ˆ¹í™µÑôˆ¤(€€€€€€€­Ý…ÉÌè‘¥ÑmÍÑÈ°½‰©•Ñt€ôì‰‰‰½á}¥¹¡•Ìˆè€‰Ñ¥¡Ð‰ô(€€€€€€€¥˜™µÐ€ôô€‰Á¹œˆè(€€€€€€€€€€€­Ý…ÉÍl‰‘Á¤‰t€ô‘Á¤(€€€€€€€™¥œ¹Í…Ù•™¥œ¡Á…Ñ °€¨©­Ý…ÉÌ¤ìÝÉ¥ÑÑ•¸¹…ÁÁ•¹¡Á…Ñ ¤(€€€Á±Ð¹±½Í”¡™¥œ¤(€€€É•ÑÕÉ¸ÝÉ¥ÑÑ•¸(()‘•˜ÝÉ¥Ñ•}µ…¹¥™•ÍÐ¡½ÕÑÁÕÑ}‘¥ÈèA…Ñ °‘…Ñ…}Á…Ñ èA…Ñ °™½¹Ñ}¹…µ”èÍÑÈ°™¥±•Ìè±¥ÍÑmA…Ñ¡t¤€´øA…Ñ è(€€€™¥ÕÉ•}‘…Ñ„€ô©Í½¸¹±½…‘Ì¡‘…Ñ…}Á…Ñ ¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤(€€€Á…å±½…€ôì(€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰ÁÕ‰±¥…Ñ¥½¹}™¥ÕÉ•Í}½µÁ±•Ñ”ˆ°(€€€€€€€€‰™¥ÕÉ•}‘…Ñ…}Ù•ÉÍ¥½¸ˆè™¥ÕÉ•}‘…Ñ…l‰Ù•ÉÍ¥½¸‰t°(€€€€€€€€‰™É½é•¹}•Ñ±}Í¡„ˆè™¥ÕÉ•}‘…Ñ…l‰™É½é•¹}•Ñ±}Í¡„‰t°(€€€€€€€€‰‘…Ñ…}Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡‘…Ñ…}Á…Ñ ¤°(€€€€€€€€‰ÍÉ¥ÁÑ}¥Ñ}Í¡„ˆè¥Ñ}Í¡„ ¤°(€€€€€€€€‰µ…ÑÁ±½Ñ±¥‰}Ù•ÉÍ¥½¸ˆèµ…ÑÁ±½Ñ±¥ˆ¹}}Ù•ÉÍ¥½¹}|°(€€€€€€€€‰™½¹Ñ}ÕÍ•ˆè™½¹Ñ}¹…µ”°(€€€€€€€€‰…É•…Ñ•}½¹±äˆèQÉÕ”°(€€€€€€€€‰™¥ÕÉ•}¹…µ•Ìˆè±¥ÍÐ¡%UI}95L¤°(€€€€€€€€‰™¥±•Ìˆèmì‰Á…Ñ ˆèÁ…Ñ ¹¹…µ”°€‰Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡Á…Ñ ¤°€‰‰åÑ•ÌˆèÁ…Ñ ¹ÍÑ…Ð ¤¹ÍÑ}Í¥é•ô™½ÈÁ…Ñ ¥¸™¥±•Ít°(€€€ô(€€€Á…Ñ €ô½ÕÑÁÕÑ}‘¥È€¼€‰ÁÕ‰±¥…Ñ¥½¹}™¥ÕÉ•Í}µ…¹¥™•ÍÐ¹©Í½¸ˆ(€€€Á…Ñ ¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡Á…å±½…°¥¹‘•¹ÐôÈ°Í½ÉÑ}­•åÌõQÉÕ”¤€¬€‰q¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€É•ÑÕÉ¸Á…Ñ (()‘•˜Á…ÉÍ•}…ÉÌ ¤€´ø…ÉÁ…ÉÍ”¹9…µ•ÍÁ…”è(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È¡‘•ÍÉ¥ÁÑ¥½¸ô‰•¹•É…Ñ”Ñ¡”…¹½¹¥…°ÁÕ‰±¥…Ñ¥½¸™¥ÕÉ•Ìˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ‘…Ñ„ˆ°ÑåÁ”õA…Ñ °‘•™…Õ±ÐõU1Q}Q¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ½ÕÑÁÕÐµ‘¥Èˆ°ÑåÁ”õA…Ñ °‘•™…Õ±ÐõA…Ñ  ‰É•ÍÕ±ÑÌ½ÁÕ‰±¥…Ñ¥½¹}…ÍÍ•ÑÌ½™¥ÕÉ•Ìˆ¤¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ™½Éµ…ÑÌˆ°‘•™…Õ±Ðô‰Á¹œ±Á‘˜±ÍÙœˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ‘Á¤ˆ°ÑåÁ”õ¥¹Ð°‘•™…Õ±ÐôÌÀÀ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ™½¹Ðˆ°¡•±Àô‰AÉ•™•ÉÉ•¥¹ÍÑ…±±•™½¹ÐìÉ¥…°½È!•±Ù•Ñ¥„É•½µµ•¹‘•ˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍÑÉ¥Ðµ™½¹Ðˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ°¡•±Àô‰…¥°Õ¹±•ÍÌÉ¥…°½È!•±Ù•Ñ¥„¥Ì…Ù…¥±…‰±”ˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ½¹±äˆ°…Ñ¥½¸ô‰…ÁÁ•¹ˆ°¡½¥•Ìõ%UI}95L¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÙ•É¥™äµ½¹±äˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ¤(€€€É•ÑÕÉ¸Á…ÉÍ•È¹Á…ÉÍ•}…ÉÌ ¤(()‘•˜µ…¥¸ ¤€´ø9½¹”è(€€€…ÉÌ€ôÁ…ÉÍ•}…ÉÌ ¤ì‘…Ñ„€ô±½…‘}‘…Ñ„¡…ÉÌ¹‘…Ñ„¤(€€€¥˜…ÉÌ¹Ù•É¥™å}½¹±äè(€€€€€€€ÁÉ¥¹Ð ‰ÍÑ…ÑÕÌèÁÕ‰±¥…Ñ¥½¹}™¥ÕÉ•}‘…Ñ…}Ù…±¥ˆ¤ìÁÉ¥¹Ð¡˜‰‘…Ñ…}Í¡„ÈÔØèíÍ¡„ÈÔÙ}™¥±”¡…ÉÌ¹‘…Ñ„¥ôˆ¤ìÉ•ÑÕÉ¸(€€€™½¹Ñ}¹…µ”€ôÉ•Í½±Ù•}™½¹Ð¡…ÉÌ¹™½¹Ð¤(€€€¥˜…ÉÌ¹ÍÑÉ¥Ñ}™½¹Ð…¹™½¹Ñ}¹…µ”¹½Ð¥¸ì‰É¥…°ˆ°€‰!•±Ù•Ñ¥„‰ôè(€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È¡˜‰¥¹…°µÍÕ‰µ¥ÍÍ¥½¸™½¹Ð¡•¬™…¥±•èÉ•Í½±Ù•í™½¹Ñ}¹…µ”…Éôì¥¹ÍÑ…±°É¥…°½È!•±Ù•Ñ¥„ˆ¤(€€€™½Éµ…ÑÌ€ôÑÕÁ±”¡¥Ñ•´¹ÍÑÉ¥À ¤¹±½Ý•È ¤™½È¥Ñ•´¥¸…ÉÌ¹™½Éµ…ÑÌ¹ÍÁ±¥Ð ˆ°ˆ¤¥˜¥Ñ•´¹ÍÑÉ¥À ¤¤(€€€¥¹Ù…±¥€ôÍ•Ð¡™½Éµ…ÑÌ¤€´ì‰Á¹œˆ°€‰Á‘˜ˆ°€‰ÍÙœ‰ô(€€€¥˜¥¹Ù…±¥è(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È¡˜‰U¹ÍÕÁÁ½ÉÑ•™½Éµ…ÑÌèíÍ½ÉÑ•¡¥¹Ù…±¥¥ôˆ¤(€€€…ÉÌ¹½ÕÑÁÕÑ}‘¥È¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤(€€€Í•±•Ñ•€ôÍ•Ð¡…ÉÌ¹½¹±ä½È%UI}95L¤ì™¥±•Ìè±¥ÍÑmA…Ñ¡t€ômt(€€€™½È¹…µ”¥¸%UI}95Lè(€€€€€€€¥˜¹…µ”¹½Ð¥¸Í•±•Ñ•è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€…ÁÁ±å}ÍÑå±”¡™½¹Ñ}¹…µ”°•áÑ•¹‘•õ¹…µ”¥¸aQ9}95L¤(€€€€€€€™¥±•Ì¹•áÑ•¹¡Í…Ù•}™¥ÕÉ”¡	U%1IMm¹…µ•t¡‘…Ñ„¤°…ÉÌ¹½ÕÑÁÕÑ}‘¥È€¼¹…µ”°™½Éµ…ÑÌ°…ÉÌ¹‘Á¤¤¤(€€€µ…¹¥™•ÍÐ€ôÝÉ¥Ñ•}µ…¹¥™•ÍÐ¡…ÉÌ¹½ÕÑÁÕÑ}‘¥È°…ÉÌ¹‘…Ñ„°™½¹Ñ}¹…µ”°™¥±•Ì¤(€€€ÁÉ¥¹Ð ‰ÍÑ…ÑÕÌèÁÕ‰±¥…Ñ¥½¹}™¥ÕÉ•Í}½µÁ±•Ñ”ˆ¤ìÁÉ¥¹Ð¡˜‰™½¹Ñ}ÕÍ•èí™½¹Ñ}¹…µ•ôˆ¤ìÁÉ¥¹Ð¡˜‰™¥ÕÉ•Í}ÝÉ¥ÑÑ•¸èí±•¸¡™¥±•Ì¥ôˆ¤ìÁÉ¥¹Ð¡˜‰µ…¹¥™•ÍÐèíµ…¹¥™•ÍÑôˆ¤(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€µ…¥¸ ¤(