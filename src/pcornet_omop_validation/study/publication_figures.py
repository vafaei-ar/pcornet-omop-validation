from __future__ import annotations

"""Generate canonical publication figures from disclosure-reviewed aggregate results."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager

DEFAULT_DATA = Path("study_definitions/artifacts/publication_figure_data_v2.json")
DEFAULT_OUTPUT = Path("results/publication_assets/figures")

COLORS = {
    "pcornet": "#0072B2",
    "omop": "#D55E00",
    "green": "#009E73",
    "dark": "#222222",
    "mid": "#777777",
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


def resolve_font(preferred: str | None = None, strict: bool = False) -> str:
    available = _available_fonts()
    if preferred:
        if preferred in available:
            return preferred
        if strict:
            raise RuntimeError(f"Requested font {preferred!r} is not installed")
    for name in ("Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"):
        if name in available:
            return name
    return "DejaVu Sans"


def apply_style(font_name: str) -> None:
    plt.rcdefaults()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [font_name, "Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
            "font.size": 9.5,
            "axes.titlesize": 11.0,
            "axes.labelsize": 9.5,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.fontsize": 8.5,
            "axes.linewidth": 0.75,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def _clean(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)


def _panel(ax: plt.Axes, letter: str) -> None:
    ax.text(-0.11, 1.06, letter, transform=ax.transAxes, fontweight="bold", fontsize=12, va="top")


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
    if numeric["direct_exact"] + numeric["explained_vital_differences"] != numeric["comparable"] or numeric["unexplained"] != 0:
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
    c = data["stage_c"]["primary"]["D0"]
    d = data["stage_d"]
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.6), gridspec_kw={"width_ratios": [1.05, 1.0]})

    ax = axes[0]
    _panel(ax, "a")
    stages = ["Mapped\nsemantics", "Independent\ncohort", "Fixed patient/\nindex outcomes", "End-to-end\noutcomes"]
    y = [1.0, 0.72, 0.44, 0.16]
    notes = [
        "Exact within locked mapped denominators",
        f"D0: {c['pcornet']:,} vs {c['omop']:,}\nJaccard {c['jaccard']:.3f}",
        "30 d and 90 d risk difference = 0",
        f"90 d risk difference = {d['end_to_end']['90_day']['risk_difference_pp']:+.2f} pp",
    ]
    for yy, label, note in zip(y, stages, notes):
        ax.text(0.04, yy, label, transform=ax.transAxes, fontweight="bold", va="top")
        ax.text(0.38, yy, note, transform=ax.transAxes, va="top")
        if yy > y[-1]:
            ax.annotate("", xy=(0.19, yy - 0.20), xytext=(0.19, yy - 0.07), xycoords=ax.transAxes,
                        arrowprops={"arrowstyle": "-|>", "lw": 1.0, "color": COLORS["mid"]})
    ax.set_title("Reproducibility breakpoint")
    ax.axis("off")

    ax = axes[1]
    _panel(ax, "b")
    labels = ["30-day", "90-day"]
    x = np.arange(2)
    fixed = [d["fixed"]["30_day"]["risk_difference_pp"], d["fixed"]["90_day"]["risk_difference_pp"]]
    end = [d["end_to_end"]["30_day"]["risk_difference_pp"], d["end_to_end"]["90_day"]["risk_difference_pp"]]
    ax.axhspan(-0.5, 0.5, color=COLORS["green_fill"], zorder=0)
    ax.scatter(x - 0.08, fixed, s=55, label="Fixed patient/index", color=COLORS["green"], zorder=3)
    ax.scatter(x + 0.08, end, s=55, label="End-to-end", color=COLORS["omop"], zorder=3)
    ax.axhline(0, lw=0.8, color=COLORS["dark"])
    ax.set_xticks(x, labels)
    ax.set_ylabel("OMOP minus PCORnet risk difference (percentage points)")
    ax.set_title("Downstream representation is exact; cohort selection changes the estimand")
    ax.legend(frameon=False, loc="upper left")
    _clean(ax)
    fig.tight_layout()
    return fig


def figure2(data: dict) -> plt.Figure:
    stage = data["stage_c"]
    keys = ("D0", "D1", "D3")
    labels = ("Base (D0)", "Principal diagnosis (D1)", "Principal + acute care (D3)")
    fig, axes = plt.subplots(1, 3, figsize=(11.2, 4.6), sharey=True)

    panels = [
        ("a", "Primary source-faithful comparison", stage["primary"], "omop"),
        ("b", "Sensitivity: require recorded diagnosis date", stage["harmonized_dxdate"], "omop"),
        ("c", "Sensitivity: encounter-date fallback in target", stage["encounter_date_fallback"], "target"),
    ]
    for ax, (letter, title, block, target_key) in zip(axes, panels):
        _panel(ax, letter)
        y = np.arange(len(keys))
        source = [block[k]["pcornet"] for k in keys]
        target = [block[k][target_key] for k in keys]
        h = 0.34
        ax.barh(y + h / 2, source, height=h, label="PCORnet", color=COLORS["pcornet"])
        ax.barh(y - h / 2, target, height=h, label="OMOP target", color=COLORS["omop"])
        for i, k in enumerate(keys):
            ax.text(max(source[i], target[i]) * 1.01, i, f"J={block[k]['jaccard']:.3f}", va="center", fontsize=8)
        ax.set_yticks(y, labels)
        ax.invert_yaxis()
        ax.set_title(title)
        ax.set_xlabel("Patients")
        _clean(ax)
    axes[0].legend(frameon=False, loc="lower right")
    fig.suptitle("The phenotype discrepancy is driven by diagnosis-date policy", y=1.01, fontsize=12.5, fontweight="bold")
    fig.tight_layout()
    return fig


def figure3(data: dict) -> plt.Figure:
    d = data["stage_d"]
    fig, axes = plt.subplots(1, 3, figsize=(11.4, 4.7))

    ax = axes[0]
    _panel(ax, "a")
    labels = ["30-day", "90-day"]
    x = np.arange(2)
    width = 0.34
    source = [d["fixed"]["30_day"]["pcornet_risk_percent"], d["fixed"]["90_day"]["pcornet_risk_percent"]]
    target = [d["fixed"]["30_day"]["omop_risk_percent"], d["fixed"]["90_day"]["omop_risk_percent"]]
    ax.bar(x - width / 2, source, width, label="PCORnet", color=COLORS["pcornet"])
    ax.bar(x + width / 2, target, width, label="OMOP", color=COLORS["omop"])
    ax.set_xticks(x, labels)
    ax.set_ylabel("Outcome risk (%)")
    ax.set_title("Fixed patient/index outcome representation")
    ax.legend(frameon=False)
    _clean(ax)

    ax = axes[1]
    _panel(ax, "b")
    comp = d["source_only_complement"]["90_day"]
    risks = [comp["shared_risk_percent"], comp["source_only_risk_percent"]]
    ax.bar([0, 1], risks, color=[COLORS["green"], COLORS["mid"]], width=0.62)
    ax.set_xticks([0, 1], [f"Shared\n(n={comp['shared_eligible']:,})", f"Source-only\n(n={comp['source_only_eligible']:,})"])
    ax.set_ylabel("90-day risk (%)")
    ax.set_title("Selective cohort loss is outcome-associated")
    ax.text(0.5, max(risks) * 0.55, f"Difference {comp['source_only_minus_shared_risk_difference_pp']:.2f} pp", ha="center", fontweight="bold")
    _clean(ax)

    ax = axes[2]
    _panel(ax, "c")
    end = [d["end_to_end"]["30_day"]["risk_difference_pp"], d["end_to_end"]["90_day"]["risk_difference_pp"]]
    fallback = [d["fallback_end_to_end"]["30_day"]["risk_difference_pp"], d["fallback_end_to_end"]["90_day"]["risk_difference_pp"]]
    ax.axhspan(-0.5, 0.5, color=COLORS["green_fill"], zorder=0)
    ax.scatter(x - 0.08, end, s=60, label="Primary end-to-end", color=COLORS["omop"])
    ax.scatter(x + 0.08, fallback, s=60, label="Encounter-date fallback", color=COLORS["green"])
    ax.axhline(0, lw=0.8, color=COLORS["dark"])
    ax.set_xticks(x, labels)
    ax.set_ylabel("OMOP minus PCORnet risk difference (pp)")
    ax.set_title("Mechanism sensitivity restores outcome agreement")
    ax.legend(frameon=False, loc="upper left")
    _clean(ax)

    fig.suptitle("Cohort selection, not outcome representation, drives the end-to-end risk drift", y=1.02, fontsize=12.5, fontweight="bold")
    fig.tight_layout()
    return fig


def figure4(data: dict) -> plt.Figure:
    e = data["stage_e"]
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.6))

    ax = axes[0]
    _panel(ax, "a")
    features = list(e["end_to_end_feature_smd"])
    end = np.array([e["end_to_end_feature_smd"][k] for k in features])
    fixed = np.array([e["fixed_feature_smd"][k] for k in features])
    y = np.arange(len(features))
    ax.axvspan(-0.10, 0.10, color=COLORS["gray_fill"])
    ax.scatter(fixed, y - 0.12, label="Fixed patient/index", color=COLORS["green"], s=38)
    ax.scatter(end, y + 0.12, label="End-to-end", color=COLORS["omop"], s=38)
    ax.axvline(0, color=COLORS["dark"], lw=0.8)
    ax.set_yticks(y, features)
    ax.invert_yaxis()
    ax.set_xlabel("Standardized mean difference")
    ax.set_title("Feature reproducibility")
    ax.legend(frameon=False)
    _clean(ax)

    ax = axes[1]
    _panel(ax, "b")
    models = list(e["models"])
    x = np.arange(len(models))
    fixed_diff = [e["models"][m]["fixed_auroc_difference"] for m in models]
    end_diff = [e["models"][m]["end_auroc_difference"] for m in models]
    ax.scatter(x - 0.08, fixed_diff, label="Fixed patient/index", color=COLORS["green"], s=48)
    ax.scatter(x + 0.08, end_diff, label="End-to-end", color=COLORS["omop"], s=48)
    ax.axhline(0, lw=0.8, color=COLORS["dark"])
    ax.set_xticks(x, [m.replace(" ", "\n", 1) for m in models])
    ax.set_ylabel("OMOP minus PCORnet AUROC")
    ax.set_title("Model performance reproducibility")
    ax.legend(frameon=False)
    _clean(ax)
    fig.tight_layout()
    return fig


def extended1(data: dict) -> plt.Figure:
    b = data["stage_b"]
    fig, axes = plt.subplots(1, 2, figsize=(10.3, 4.5))
    ax = axes[0]
    _panel(ax, "a")
    labels = list(b["mapped_exact_counts"])
    values = np.array(list(b["mapped_exact_counts"].values()), dtype=float)
    ax.barh(np.arange(len(labels)), values / 1e6, color=COLORS["pcornet"])
    ax.set_yticks(np.arange(len(labels)), labels)
    ax.invert_yaxis()
    ax.set_xlabel("Exactly reconciled mapped records (millions)")
    ax.set_title("Mapped semantic fidelity")
    _clean(ax)

    ax = axes[1]
    _panel(ax, "b")
    n = b["numeric"]
    vals = [n["direct_exact"], n["explained_vital_differences"], n["unexplained"]]
    labs = ["Direct exact", "Explained vital-policy differences", "Unexplained"]
    ax.barh(np.arange(3), np.array(vals) / 1e6, color=[COLORS["green"], COLORS["mid"], COLORS["omop"]])
    ax.set_yticks(np.arange(3), labs)
    ax.invert_yaxis()
    ax.set_xlabel("Comparable numeric records (millions)")
    ax.set_title("Numeric reconciliation")
    _clean(ax)
    fig.tight_layout()
    return fig


def extended2(data: dict) -> plt.Figure:
    d = data["stage_d"]
    r = d["recurrent"]
    fig, axes = plt.subplots(1, 2, figsize=(9.8, 4.4))
    ax = axes[0]
    _panel(ax, "a")
    labels = ["Primary recurrent", "Principal-diagnosis sensitivity"]
    source = [r["primary_pcornet_events"], r["pdx_primary_sensitivity_pcornet_events"]]
    target = [r["primary_omop_events"], r["pdx_primary_sensitivity_omop_events"]]
    x = np.arange(2); w = 0.34
    ax.bar(x - w / 2, source, w, color=COLORS["pcornet"], label="PCORnet")
    ax.bar(x + w / 2, target, w, color=COLORS["omop"], label="OMOP")
    ax.set_xticks(x, [s.replace(" ", "\n", 1) for s in labels])
    ax.set_ylabel("Events")
    ax.set_title("Recurrent stroke outcome")
    ax.legend(frameon=False)
    _clean(ax)

    ax = axes[1]
    _panel(ax, "b")
    windows = ["30_day", "90_day"]
    rrs = [d["end_to_end"][w]["rr"] for w in windows]
    ax.axhspan(d["reproducibility_tolerances"]["rr_lower"], d["reproducibility_tolerances"]["rr_upper"], color=COLORS["green_fill"])
    ax.scatter([0, 1], rrs, color=COLORS["omop"], s=55)
    ax.axhline(1.0, color=COLORS["dark"], lw=0.8)
    ax.set_xticks([0, 1], ["30-day", "90-day"])
    ax.set_ylabel("OMOP / PCORnet risk ratio")
    ax.set_title("End-to-end relative-risk drift")
    _clean(ax)
    fig.tight_layout()
    return fig


def extended3(data: dict) -> plt.Figure:
    e = data["stage_e"]["calibration"]
    models = list(e)
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.5))

    ax = axes[0]
    _panel(ax, "a")
    x = np.arange(len(models))
    src = [e[m]["fixed"]["pcornet_slope"] for m in models]
    tgt = [e[m]["fixed"]["omop_slope"] for m in models]
    ax.scatter(x - 0.08, src, label="PCORnet", color=COLORS["pcornet"], s=45)
    ax.scatter(x + 0.08, tgt, label="OMOP", color=COLORS["omop"], s=45)
    ax.axhline(1.0, color=COLORS["mid"], lw=0.8)
    ax.set_xticks(x, [m.replace(" ", "\n", 1) for m in models])
    ax.set_ylabel("Calibration slope")
    ax.set_title("Fixed patient/index")
    ax.legend(frameon=False)
    _clean(ax)

    ax = axes[1]
    _panel(ax, "b")
    src = [e[m]["end_to_end"]["pcornet_slope"] for m in models]
    tgt = [e[m]["end_to_end"]["omop_slope"] for m in models]
    ax.scatter(x - 0.08, src, label="PCORnet", color=COLORS["pcornet"], s=45)
    ax.scatter(x + 0.08, tgt, label="OMOP", color=COLORS["omop"], s=45)
    ax.axhline(1.0, color=COLORS["mid"], lw=0.8)
    ax.set_xticks(x, [m.replace(" ", "\n", 1) for m in models])
    ax.set_ylabel("Calibration slope")
    ax.set_title("End-to-end")
    ax.legend(frameon=False)
    _clean(ax)
    fig.tight_layout()
    return fig


BUILDERS = {
    "Figure1_reproducibility_breakpoint": figure1,
    "Figure2_phenotype_mechanism": figure2,
    "Figure3_outcome_estimands": figure3,
    "Figure4_model_reproducibility": figure4,
    "ExtendedDataFigure1_semantic_fidelity": extended1,
    "ExtendedDataFigure2_additional_reproducibility": extended2,
    "ExtendedDataFigure3_calibration": extended3,
}


def _git_sha() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def render(data_path: Path, output_dir: Path, names: list[str], formats: list[str], dpi: int, font: str | None, strict_font: bool) -> None:
    data = load_data(data_path)
    font_name = resolve_font(font, strict_font)
    apply_style(font_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    for name in names:
        fig = BUILDERS[name](data)
        for fmt in formats:
            kwargs = {"bbox_inches": "tight"}
            if fmt == "png":
                kwargs["dpi"] = dpi
            fig.savefig(output_dir / f"{name}.{fmt}", **kwargs)
        plt.close(fig)

    manifest = {
        "figure_data_version": data["version"],
        "frozen_etl_sha": data["frozen_etl_sha"],
        "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
        "git_sha": _git_sha(),
        "font": font_name,
        "figures": names,
        "formats": formats,
        "dpi": dpi,
    }
    (output_dir / "figure_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--formats", default="png,pdf,svg")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--font")
    p.add_argument("--strict-font", action="store_true")
    p.add_argument("--only", action="append", choices=FIGURE_NAMES)
    p.add_argument("--verify-only", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    data = load_data(args.data)
    if args.verify_only:
        print(f"Publication figure data verified: {data['version']}")
        return
    names = args.only or list(FIGURE_NAMES)
    formats = [x.strip().lower() for x in args.formats.split(",") if x.strip()]
    unsupported = [x for x in formats if x not in {"png", "pdf", "svg"}]
    if unsupported:
        raise SystemExit(f"Unsupported formats: {', '.join(unsupported)}")
    render(args.data, args.output_dir, names, formats, args.dpi, args.font, args.strict_font)
    print(f"Generated {len(names)} figures in {args.output_dir}")


if __name__ == "__main__":
    main()
