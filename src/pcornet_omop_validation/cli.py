from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .profiling import ProfileConfig, run_profile


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Profile paired PCORnet and OMOP parquet datasets.")
    p.add_argument("--config", type=Path, help="YAML configuration file")
    p.add_argument("--pcornet", type=Path, help="Directory containing PCORnet parquet files")
    p.add_argument("--omop", type=Path, help="Directory containing OMOP parquet files")
    p.add_argument("--output", type=Path, default=Path("results"), help="Output directory")
    p.add_argument("--minimum-cell-size", type=int, default=11)
    p.add_argument("--top-n-categories", type=int, default=25)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    values = {}
    if args.config:
        values = yaml.safe_load(args.config.read_text()) or {}
    pcornet = args.pcornet or (Path(values["pcornet_dir"]) if values.get("pcornet_dir") else None)
    omop = args.omop or (Path(values["omop_dir"]) if values.get("omop_dir") else None)
    output = args.output if args.output != Path("results") or not values.get("output_dir") else Path(values["output_dir"])
    minimum = args.minimum_cell_size if args.minimum_cell_size != 11 else int(values.get("minimum_cell_size", 11))
    top_n = args.top_n_categories if args.top_n_categories != 25 else int(values.get("top_n_categories", 25))
    if pcornet is None or omop is None:
        raise SystemExit("Provide --pcornet and --omop, or a --config containing both paths.")
    for label, path in (("PCORnet", pcornet), ("OMOP", omop)):
        if not path.is_dir():
            raise SystemExit(f"{label} directory does not exist: {path}")
    out = run_profile(ProfileConfig(pcornet, omop, output, minimum, top_n))
    print(f"Profile complete: {out.resolve()}")


if __name__ == "__main__":
    main()
