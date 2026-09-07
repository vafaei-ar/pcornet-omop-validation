#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATA="study_definitions/artifacts/publication_figure_data_v1.json"
FIG_DIR="results/publication_assets/figures"
TAB_DIR="results/publication_assets/tables"

# Remove obsolete local output layouts from earlier development iterations.
rm -rf \
  figures/jamia \
  figures/jamia_v2 \
  figures/jamia_v3 \
  figures/jamia_extended \
  tables/jamia \
  "$FIG_DIR" \
  "$TAB_DIR"

python -m pcornet_omop_validation.study.publication_figures \
  --data "$DATA" \
  --verify-only

python -m pcornet_omop_validation.study.publication_figures \
  --data "$DATA" \
  --output-dir "$FIG_DIR"

python -m pcornet_omop_validation.study.publication_tables \
  --data "$DATA" \
  --outdir "$TAB_DIR"

printf '\nPublication assets rebuilt successfully.\n'
printf 'Figures: %s\n' "$FIG_DIR"
printf 'Tables:  %s\n' "$TAB_DIR"
printf '\nGenerated figure files:\n'
find "$FIG_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
printf '\nGenerated table files:\n'
find "$TAB_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
