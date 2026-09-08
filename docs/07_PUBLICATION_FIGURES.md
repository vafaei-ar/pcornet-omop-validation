# 07 — Publication figures and tables

This document defines the **single current publication-asset pipeline** for the study. Historical plotting implementations were removed from the current branch; Git history preserves them if provenance review is ever needed.

The rule is simple:

> **All manuscript figures are generated from code in `publication_figures.py` using disclosure-reviewed aggregate publication artifacts. No figure should be manually edited after export.**

## Scientific source of truth

The current figure input is:

`study_definitions/artifacts/publication_figure_data_v2.json`

Version 2 retains the frozen publication aggregates and adds disclosure-reviewed aggregate results from the post-freeze mechanism analyses: the encounter-date fallback sensitivity and the Stage D source-only complement audit. It contains no patient identifiers, row-level predictions, or protected health information.

The original frozen publication artifact remains unchanged for provenance:

`study_definitions/artifacts/publication_figure_data_v1.json`

Its SHA-256 is tested so later figure revisions cannot silently rewrite the original publication snapshot.

The figure runner validates locked scientific invariants before rendering, including:

- exact concordance of the recorded-diagnosis-date D0/D1/D3 sensitivity;
- exact D0/D1/D3 concordance under the encounter-admission-date fallback sensitivity;
- exact fixed-cohort Stage D event/risk agreement;
- exact 30-day and 90-day fallback Stage D outcome agreement;
- complete Stage B numeric reconciliation with zero unexplained differences.

## Canonical figure code

There is one publication figure module:

`src/pcornet_omop_validation/study/publication_figures.py`

It contains the current builders for all seven figures:

1. `Figure1_reproducibility_breakpoint`
2. `Figure2_phenotype_mechanism`
3. `Figure3_outcome_estimands`
4. `Figure4_model_reproducibility`
5. `ExtendedDataFigure1_semantic_fidelity`
6. `ExtendedDataFigure2_additional_reproducibility`
7. `ExtendedDataFigure3_calibration`

The current main figures use reader-facing clinical/scientific terminology rather than requiring readers to decode raw database column names. Exact schema identifiers remain in the ETL/analysis code and technical provenance records.

## Generate all figures and tables from a clean checkout

Install the plotting dependencies:

```bash
python -m pip install -e '.[figures]'
```

Then run:

```bash
bash scripts/rebuild_publication_assets.sh
```

The script uses the v2 aggregate artifact, rebuilds the canonical figures and tables, and checks Git status before and after execution. If generation changes the tracked/untracked worktree state, it exits with an error instead of silently leaving repository debris.

Default generated locations are:

- `results/publication_assets/figures/`
- `results/publication_assets/tables/`

These directories are intentionally ignored by Git. Code plus committed aggregate inputs are the source of truth.

You can also run the figure module directly:

```bash
python -m pcornet_omop_validation.study.publication_figures
```

## Validate without rendering

```bash
python -m pcornet_omop_validation.study.publication_figures --verify-only
```

This validates the current aggregate artifact and scientific invariants without writing graphics.

## Generate selected figures

For example:

```bash
python -m pcornet_omop_validation.study.publication_figures \
  --only Figure2_phenotype_mechanism \
  --only Figure3_outcome_estimands
```

## Place Figures 2 and 3 into the tracked manuscript

After generating the figures, use:

```bash
python scripts/place_publication_figures.py \
  --manuscript /path/to/Main_Manuscript_v1.3_JAMIA_reader_focus_tracked.docx \
  --figure-dir results/publication_assets/figures \
  --output /path/to/Main_Manuscript_v1.4_JAMIA_figures_updated_tracked.docx
```

The placement utility:

- copies the manuscript to a new file and never overwrites the source;
- preserves existing OOXML revision/Track Changes markup;
- replaces both PNG and SVG artwork referenced by the Figure 2 and Figure 3 drawings;
- updates drawing height to match the generated figure aspect ratio;
- refuses to write an unignored DOCX inside the repository;
- verifies that Git worktree status is unchanged before and after placement.

For a guaranteed clean repository, write the output outside the repository (for example under `~/Downloads/`) or under an already ignored directory such as `results/`.

## Font and export controls

The pipeline prefers Arial or Helvetica when installed, with a sans-serif fallback for review rendering.

For final artwork, a machine with Arial or Helvetica can enforce the font explicitly:

```bash
python -m pcornet_omop_validation.study.publication_figures \
  --font Arial \
  --strict-font
```

Change formats or PNG resolution if required by the submission system:

```bash
python -m pcornet_omop_validation.study.publication_figures \
  --formats pdf,svg,png \
  --dpi 300
```

The repository does not distribute font files.

## Reproducibility manifest

Each figure run writes:

`results/publication_assets/figures/publication_figures_manifest.json`

The manifest records:

- figure-data version;
- frozen ETL SHA;
- SHA-256 of the aggregate figure-data artifact;
- Git SHA of the current checkout;
- Matplotlib version;
- font used;
- figure names;
- every generated filename, byte size, and SHA-256.

## Current figure interpretation guardrails

**Figure 1:** summarizes the paper's main distinction: mapped technical fidelity can coexist with end-to-end study divergence when cohort selection changes upstream.

**Figure 2:** the primary source-faithful phenotype comparison remains primary. Two post-freeze mechanism sensitivities are shown: restricting source eligibility to recorded diagnosis dates and, separately, preserving missing-date diagnosis evidence in the target using the linked encounter admission date with provenance. Both restore exact D0/D1/D3 membership and index dates. Neither establishes the fallback policy as uniquely correct or standard.

**Figure 3:** fixed patient/index outcome representation is exact. The source-only D0 complement has lower 90-day risk than the shared cohort, demonstrating outcome-associated selective cohort loss. The primary end-to-end risk drift exceeds the prespecified empirical cross-CDM reproducibility tolerance, while the encounter-date fallback sensitivity restores exact 30-day and 90-day outcome agreement. The ±0.5 percentage-point band is not a clinical equivalence or noninferiority margin.

**Figure 4:** the 0.10 SMD line is a conventional descriptive reference value, not a prespecified statistical threshold. Fixed-cohort and end-to-end model results answer different questions.

**Extended Data Figure 1:** mapped semantic agreement and mapping/coverage limitations are shown separately so unresolved or unmapped records are not misclassified as mapped-event disagreement.

**Extended Data Figure 2:** panels a and b use restricted ranges to make near-exact agreement visible. Panel c retains the recurrent-stroke sensitivity as a secondary analysis and identifies PCORnet and OMOP explicitly.

**Extended Data Figure 3:** calibration slope and intercept are descriptive reproducibility results; no calibration-equivalence margin was prespecified.

## Publication tables

The table generator continues to produce the current main and supplementary table specifications from the aggregate artifact:

```bash
python -m pcornet_omop_validation.study.publication_tables \
  --data study_definitions/artifacts/publication_figure_data_v2.json \
  --outdir results/publication_assets/tables
```

## Do not create parallel figure pipelines

If a figure needs revision, modify `publication_figures.py`, regenerate the figure, and inspect the new output. Do **not** create `v2`, `v3`, `final`, `jamia`, or other parallel plotting modules/directories for incremental revisions. Git history already provides version provenance.
