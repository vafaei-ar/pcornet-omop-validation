#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATA="study_definitions/artifacts/publication_figure_data_v2.json"
FIG_DIR="results/publication_assets/figures"
TAB_DIR="results/publication_assets/tables"

START_STATUS="$(git status --porcelain=v1 --untracked-files=all)"
check_worktree_unchanged() {
  rc=$?
  END_STATUS="$(git status --porcelain=v1 --untracked-files=all)"
  if [[ "$END_STATUS" != "$START_STATUS" ]]; then
    printf '\nERROR: publication rebuild changed Git worktree status.\n' >&2
    printf '%s\n' '--- before ---' "$START_STATUS" '--- after ---' "$END_STATUS" >&2
    exit 97
  fi
  exit "$rc"
}
trap check_worktree_unchanged EXIT

# Remove obsolete local output layouts from earlier development iterations.
# All generated output locations below are ignored by Git.
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
printf '\nGit worktree status unchanged by rebuild.\n'
