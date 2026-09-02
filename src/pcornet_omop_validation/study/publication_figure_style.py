from __future__ import annotations

"""Shared Nature-oriented styling and export helpers for publication figures."""

import hashlib
import subprocess
import warnings
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

MM_PER_INCH = 25.4
DOUBLE_COLUMN_MM = 183.0
SINGLE_COLUMN_MM = 89.0

# Okabe-Ito color-vision-deficiency-friendly palette.
COLORS = {
    "pcornet": "#0072B2",
    "omop": "#D55E00",
    "harmonized": "#009E73",
    "primary": "#4D4D4D",
    "fixed": "#0072B2",
    "end": "#D55E00",
    "accent": "#CC79A7",
    "light_blue": "#DCEEF8",
    "light_orange": "#F8E7DD",
    "light_green": "#DDF2EA",
    "light_gray": "#ECECEC",
    "mid_gray": "#9A9A9A",
    "dark": "#222222",
    "grid": "#D9D9D9",
}


def mm(value: float) -> float:
    return value / MM_PER_INCH


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def resolve_font(requested: str | None = None) -> str:
    """Prefer Arial/Helvetica; use standard metrically compatible fallbacks for review."""
    candidates = [requested] if requested else []
    candidates += ["Arial", "Helvetica", "Nimbus Sans", "Liberation Sans"]
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in candidates:
        if candidate and candidate in available:
            if candidate not in {"Arial", "Helvetica"}:
                warnings.warn(
                    f"Using {candidate!r}; final Nature artwork should preferably be "
                    "regenerated with Arial or Helvetica installed.",
                    RuntimeWarning,
                )
            return candidate
    warnings.warn(
        "No Arial/Helvetica-compatible font found; using DejaVu Sans for review only.",
        RuntimeWarning,
    )
    return "DejaVu Sans"


def apply_nature_style(font_name: str) -> None:
    """Set final-size typography, strokes, editable vector text, and white background."""
    matplotlib.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [font_name],
            "font.size": 6.0,
            "axes.titlesize": 7.0,
            "axes.labelsize": 6.5,
            "axes.linewidth": 0.6,
            "axes.edgecolor": COLORS["dark"],
            "axes.labelcolor": COLORS["dark"],
            "xtick.labelsize": 5.5,
            "ytick.labelsize": 5.5,
            "xtick.major.width": 0.5,
            "ytick.major.width": 0.5,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "legend.fontsize": 5.5,
            "legend.frameon": False,
            "lines.linewidth": 0.7,
            "lines.markersize": 4.0,
            "patch.linewidth": 0.6,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.transparent": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "svg.hashsalt": "pcornet-omop-publication-v1",
        }
    )


def clean_axis(ax: plt.Axes, grid_axis: str | None = None) -> None:
    """Use a restrained Nature-style axis without background gridlines."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    # Nature explicitly advises against background gridlines. ``grid_axis`` is kept in
    # the signature so older panel code remains readable, but grids are always disabled.
    ax.grid(False)


def panel_label(ax: plt.Axes, letter: str, x: float = -0.10, y: float = 1.06) -> None:
    ax.text(
        x, y, letter, transform=ax.transAxes, fontsize=8, fontweight="bold",
        va="top", ha="left", color=COLORS["dark"], gid="panel-label"
    )


def direct_value(ax: plt.Axes, x: float, y: float, text: str, color: str, dx: float = 4) -> None:
    """Annotate a plotted value in black; ``color`` is accepted for API compatibility."""
    del color
    ax.annotate(
        text, (x, y), xytext=(dx, 0), textcoords="offset points", va="center",
        ha="left" if dx >= 0 else "right", fontsize=5.3, color=COLORS["dark"],
        annotation_clip=False, clip_on=False,
    )


def add_box(
    ax: plt.Axes,
    xy: tuple[float, float],
    width: float,
    height: float,
    text: str,
    *,
    facecolor: str = "white",
    edgecolor: str = COLORS["dark"],
    fontsize: float = 6.0,
    weight: str = "normal",
) -> None:
    x, y = xy
    ax.add_patch(
        FancyBboxPatch(
            (x, y), width, height, boxstyle="round,pad=0.006,rounding_size=0.008",
            facecolor=facecolor, edgecolor=edgecolor, linewidth=0.65,
        )
    )
    ax.text(
        x + width / 2, y + height / 2, text, ha="center", va="center",
        fontsize=fontsize, fontweight=weight, color=COLORS["dark"], linespacing=1.15,
    )


def arrow(
    ax: plt.Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    color: str = COLORS["dark"],
) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start, end, arrowstyle="-|>", mutation_scale=6, linewidth=0.65,
            color=color, shrinkA=0, shrinkB=0,
        )
    )


def validate_figure_artwork(fig: plt.Figure) -> None:
    """Enforce final-size Nature constraints before export."""
    width_mm, height_mm = [v * MM_PER_INCH for v in fig.get_size_inches()]
    if width_mm > DOUBLE_COLUMN_MM + 0.2 or height_mm > 170.2:
        raise ValueError(f"Figure exceeds 183 x 170 mm envelope: {width_mm:.1f} x {height_mm:.1f} mm")
    for text in fig.findobj(match=lambda x: isinstance(x, matplotlib.text.Text)):
        if not text.get_text().strip():
            continue
        size = float(text.get_fontsize())
        if text.get_gid() == "panel-label":
            if abs(size - 8.0) > 1e-6:
                raise ValueError(f"Panel label must be 8 pt: {text.get_text()!r}")
        elif not (5.0 <= size <= 7.0):
            raise ValueError(f"Non-panel text outside 5-7 pt ({size}): {text.get_text()!r}")
    if any(ax.images for ax in fig.axes):
        raise ValueError("Unexpected raster image embedded in vector publication figure")


def save_figure(fig: plt.Figure, stem: Path, formats: tuple[str, ...], dpi: int) -> list[Path]:
    validate_figure_artwork(fig)
    paths: list[Path] = []
    for fmt in formats:
        path = stem.with_suffix(f".{fmt}")
        metadata = {"Creator": "pcornet-omop-validation", "Date": None}
        kwargs = {"metadata": metadata} if fmt in {"pdf", "svg"} else {}
        fig.savefig(path, dpi=dpi, **kwargs)
        paths.append(path)
    plt.close(fig)
    return paths
