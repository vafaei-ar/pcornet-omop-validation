from __future__ import annotations

"""Generate all publication figures from versioned disclosure-reviewed aggregate data."""

import argparse
import json
from pathlib import Path

import matplotlib

from .publication_figure_panels_extended import EXTENDED_FIGURE_BUILDERS
from .publication_figure_panels_main import MAIN_FIGURE_BUILDERS
from .publication_figure_style import (
    DOUBLE_COLUMN_MM, EXTENDED_DATA_WIDTH_MM, MAX_FIGURE_HEIGHT_MM, SINGLE_COLUMN_MM,
    apply_nature_style, git_sha, resolve_font, save_figure, sha256_file,
)

BUILDERS = {**MAIN_FIGURE_BUILDERS, **EXTENDED_FIGURE_BUILDERS}
FIGURES = tuple(BUILDERS)


def validate_data(data: dict) -> None:
    """Fail if frozen scientific invariants are inconsistent with the figure input."""
    if data.get("version") != "publication_figure_data_v1":
        raise ValueError("Unexpected publication figure-data version")
    for phenotype in ("D0", "D1", "D3"):
        h = data["stage_c"]["harmonized_dxdate"][phenotype]
        if not (h["pcornet"] == h["omop"] == h["shared"] and h["source_only"] == h["omop_only"] == 0 and h["jaccard"] == 1.0):
            raise ValueError(f"Harmonized Stage C invariant failed for {phenotype}")
    for window in ("30_day", "90_day"):
        f = data["stage_d"]["fixed"][window]
        if not (f["pcornet_events"] == f["omop_events"] and f["pcornet_risk_percent"] == f["omop_risk_percent"] and f["risk_difference_pp"] == 0 and f["rr"] == 1):
            raise ValueError(f"Fixed Stage D invariant failed for {window}")
    n = data["stage_b"]["numeric"]
    if n["direct_exact"] + n["explained_vital_differences"] != n["comparable"] or n["unexplained"] != 0:
        raise ValueError("Stage B numeric reconciliation invariant failed")


def load_data(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8")); validate_data(data); return data


def write_manifest(output_dir: Path, data_path: Path, font_name: str, files: list[Path]) -> Path:
    payload = {
        "status": "publication_figures_complete",
        "figure_data_version": "publication_figure_data_v1",
        "frozen_etl_sha": json.loads(data_path.read_text(encoding="utf-8"))["frozen_etl_sha"],
        "data_sha256": sha256_file(data_path),
        "script_git_sha": git_sha(),
        "matplotlib_version": matplotlib.__version__,
        "font_used": font_name,
        "nature_target": {
            "main_single_column_width_mm": SINGLE_COLUMN_MM,
            "main_double_column_width_mm": DOUBLE_COLUMN_MM,
            "extended_data_max_width_mm": EXTENDED_DATA_WIDTH_MM,
            "max_height_mm": MAX_FIGURE_HEIGHT_MM,
            "body_font_pt": "5-7",
            "panel_label_pt": 8,
            "line_weight_pt": "0.25-1",
            "vector_text_editable": True,
        },
        "files": [{"path": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size} for p in files],
        "aggregate_only": True,
    }
    path = output_dir / "publication_figures_manifest.json"; path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"); return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate Nature-style publication figures")
    p.add_argument("--data", type=Path, default=Path("study_definitions/artifacts/publication_figure_data_v1.json"))
    p.add_argument("--output-dir", type=Path, default=Path("figures/generated"))
    p.add_argument("--formats", default="pdf,eps,svg,png")
    p.add_argument("--dpi", type=int, default=600)
    p.add_argument("--font", help="Preferred installed font; Arial or Helvetica recommended")
    p.add_argument("--strict-font", action="store_true", help="Fail unless Arial or Helvetica is available")
    p.add_argument("--only", action="append", choices=FIGURES)
    p.add_argument("--verify-only", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args(); data = load_data(args.data)
    if args.verify_only:
        print("status: publication_figure_data_valid"); print(f"data_sha256: {sha256_file(args.data)}"); return
    font_name = resolve_font(args.font)
    if args.strict_font and font_name not in {"Arial", "Helvetica"}:
        raise RuntimeError(f"Final-submission font check failed: resolved {font_name!r}; install Arial or Helvetica")
    apply_nature_style(font_name)
    formats = tuple(x.strip().lower() for x in args.formats.split(",") if x.strip())
    invalid = set(formats) - {"svg", "pdf", "eps", "png"}
    if invalid: raise ValueError(f"Unsupported formats: {sorted(invalid)}")
    output_dir = args.output_dir; output_dir.mkdir(parents=True, exist_ok=True); selected = set(args.only or [])
    files: list[Path] = []
    for name, builder in BUILDERS.items():
        if selected and name not in selected: continue
        files.extend(save_figure(builder(data), output_dir / name, formats, args.dpi))
    manifest = write_manifest(output_dir, args.data, font_name, files)
    print("status: publication_figures_complete"); print(f"font_used: {font_name}"); print(f"figures_written: {len(files)}"); print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
