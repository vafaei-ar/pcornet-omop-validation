from __future__ import annotations

"""Final JAMIA figure export wrapper with post-render collision corrections.

The base figure construction lives in publication_jamia_assets.py. This wrapper keeps
those aggregate-only builders reproducible while applying the final visually reviewed
annotation positions used in the manuscript submission package.
"""

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt

from pcornet_omop_validation.study import publication_jamia_assets as base


def _figure2(data: dict) -> plt.Figure:
    fig = base.figure2_phenotype_mechanism(data)
    # In panel b, labels below D0/D1 are clear; D3 needs to sit above the baseline.
    ax = fig.axes[1]
    for text in ax.texts:
        x, y = text.get_position()
        if text.get_text() in {"0.622", "1.000"} and y < 0:
            text.set_position((x, 0.16))
            text.set_verticalalignment("bottom")
    return fig


def _figure3(data: dict) -> plt.Figure:
    fig = base.figure3_outcome_estimands(data)
    ax = fig.axes[1]
    ax.set_xlim(14, 37)
    for text in ax.texts:
        label = text.get_text()
        if label.startswith("Δ +1.03"):
            text.set_position((22.0, 0.88))
            text.set_horizontalalignment("left")
            text.set_verticalalignment("center")
        elif label.startswith("Δ +1.99"):
            text.set_position((30.8, 0.12))
            text.set_horizontalalignment("left")
            text.set_verticalalignment("center")
        elif label == "b":
            text.set_position((-0.14, 1.10))
    return fig


def _figure4(data: dict) -> plt.Figure:
    fig = base.figure4_model_reproducibility(data)
    ax = fig.axes[1]
    for text in ax.texts:
        label = text.get_text()
        x, y = text.get_position()
        if label == "b":
            text.set_position((-0.16, 1.12))
        elif label == "-0.04" and y > 1.5:
            text.set_position((x, 1.83))
            text.set_verticalalignment("center")
    return fig


def _extended1(data: dict) -> plt.Figure:
    fig = base.extended1_semantic_fidelity(data)
    ax_a, _ax_b, ax_c = fig.axes[:3]

    # Keep long semantic labels readable without allowing them to spill into adjacent panels.
    ax_a.set_yticklabels([
        "Encounter",
        "Death",
        "Condition",
        "Procedure",
        "Drug",
        "Measurement/\nObservation",
    ])
    ax_c.set_yticklabels([
        "Condition\nunmapped",
        "Procedure\nunresolved",
        "Drug\nunmapped",
        "Measurement/observation\nunresolved",
    ])
    ax_c.tick_params(axis="y", pad=2)
    fig.subplots_adjust(wspace=.58)
    return fig


def _extended2(data: dict) -> plt.Figure:
    fig = base.extended2_additional_reproducibility(data)
    ax_a, ax_b, ax_c = fig.axes[:3]

    # Raise panel titles away from data/annotations while preserving large readable type.
    fig.subplots_adjust(top=.90, wspace=.52)
    for ax in (ax_a, ax_b, ax_c):
        ax.set_title(ax.get_title(), pad=15)

    # The top logistic-correlation annotation should sit below the title, closer to its point.
    for text in ax_b.texts:
        x, y = text.get_position()
        if text.get_text() == ">0.999" and y > 1.5:
            text.set_position((x, 1.92))
            text.set_verticalalignment("top")

    # Use reader-facing terminology and give both recurrent-event rows adequate vertical room.
    ax_c.set_yticklabels([
        "Primary recurrent\nstroke-code endpoint",
        "Post-outcome principal-diagnosis\nsensitivity",
    ])
    ax_c.set_ylim(-.18, 1.18)

    # Keep the lower OMOP value label inside the plotting region instead of the x-axis ticks.
    for text in ax_c.texts:
        x, y = text.get_position()
        if text.get_text() == "170" and y < 0:
            text.set_position((x + 4, -0.05))
            text.set_horizontalalignment("left")
            text.set_verticalalignment("center")

    # Add the missing identity legend in the low-information lower-right corner.
    ax_c.legend(
        handles=[
            plt.Line2D([], [], marker="o", mfc="white", mec=base.COLORS["pcornet"], ls="None", label="PCORnet"),
            plt.Line2D([], [], marker="o", color=base.COLORS["omop"], ls="None", label="OMOP"),
        ],
        loc="lower right",
        frameon=False,
        handletextpad=.4,
    )
    return fig


def _save(fig: plt.Figure, stem: Path) -> None:
    fig.savefig(stem.with_suffix(".png"), dpi=300, bbox_inches="tight")
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(stem.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="study_definitions/artifacts/publication_figure_data_v1.json")
    parser.add_argument("--outdir", default="figures/jamia")
    args = parser.parse_args()
    base._style()
    data = json.loads(Path(args.data).read_text())
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    builders = {
        "Figure1_reproducibility_breakpoint": base.figure1_reproducibility_breakpoint,
        "Figure2_phenotype_mechanism": _figure2,
        "Figure3_outcome_estimands": _figure3,
        "Figure4_model_reproducibility": _figure4,
        "ExtendedDataFigure1_semantic_fidelity": _extended1,
        "ExtendedDataFigure2_additional_reproducibility": _extended2,
        "ExtendedDataFigure3_calibration": base.extended3_calibration,
    }
    for name, builder in builders.items():
        _save(builder(data), out / name)


if __name__ == "__main__":
    main()
