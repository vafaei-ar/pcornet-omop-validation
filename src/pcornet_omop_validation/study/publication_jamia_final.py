from __future__ import annotations

"""Final JAMIA figure export wrapper with post-render collision corrections.

The base figure construction lives in publication_jamia_assets.py.  This wrapper keeps
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
        "ExtendedDataFigure1_semantic_fidelity": base.extended1_semantic_fidelity,
        "ExtendedDataFigure2_additional_reproducibility": base.extended2_additional_reproducibility,
        "ExtendedDataFigure3_calibration": base.extended3_calibration,
    }
    for name, builder in builders.items():
        _save(builder(data), out / name)


if __name__ == "__main__":
    main()
