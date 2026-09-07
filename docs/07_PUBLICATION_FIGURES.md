# 07 — Publication figures and tables

This document defines the **single current publication-asset pipeline** for the study. Historical plotting implementations were removed from the current branch; Git history preserves them if provenance review is ever needed.

The rule is simple:

> **All manuscript figures are generated from code in `publication_figures.py` using the frozen aggregate publication artifact. No figure should be manually edited after export.**

## Scientific source of truth

Figure inputs are stored in:

`study_definitions/artifacts/publication_figure_data_v1.json`

This committed artifact contains disclosure-reviewed aggregate values only. It does not contain patient identifiers, row-level predictions, or protected health information.

The figure runner validates locked scientific invariants before rendering, including:

- exact concordance of the harmonized D0/D1/D3 sensitivity;
- exact fixed-cohort Stage D event/risk agreement;
- complete Stage B numeric reconciliation with zero unexplained differences.

## Canonical figure code

There is now one publication figure module:

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

## Generate all figures

Install the plotting dependencies:

```bash
python -m pip install -e '.[figures]'
```

Then run either:

```bash
pcornet-omop-figures
```

or the equivalent module command:

```bash
python -m pcornet_omop_validation.study.publication_figures
```

The default output directory is:

`results/publication_assets/figures/`

The default formats are PNG, PDF, and SVG. Generated outputs are intentionally ignored by Git; the code plus frozen aggregate input are the source of truth.

To regenerate from a clean output directory:

```bash
rm -rf results/publication_assets/figures
pcornet-omop-figures
```

## Validate without rendering

```bash
pcornet-omop-figures --verify-only
```

This validates the frozen aggregate artifact and scientific invariants without writing graphics.

## Generate selected figures

For example:

```bash
pcornet-omop-figures \
  --only Figure2_phenotype_mechanism \
  --only ExtendedDataFigure2_additional_reproducibility
```

## Font and export controls

The pipeline prefers Arial or Helvetica when installed, with a sans-serif fallback for review rendering.

For final artwork, a machine with Arial or Helvetica can enforce the font explicitly:

```bash
pcornet-omop-figures --font Arial --strict-font
```

Change formats or PNG resolution if required by the eventual submission system:

```bash
pcornet-omop-figures --formats pdf,svg,png --dpi 300
```

The repository does not distribute font files.

## Reproducibility manifest

Each figure run writes:

`results/publication_assets/figures/publication_figures_manifest.json`

The manifest records:

- frozen ETL SHA;
- SHA-256 of the aggregate figure-data artifact;
- Git SHA of the current checkout;
- Matplotlib version;
- font used;
- figure names;
- every generated filename, byte size, and SHA-256.

## Current figure interpretation guardrails

**Figure 1:** summarizes the paper's main distinction: mapped technical fidelity can coexist with end-to-end study divergence when cohort selection changes upstream.

**Figure 2:** the diagnosis-date harmonization is a post-freeze sensitivity. It explains the mechanism but does not replace the source-faithful primary phenotype comparison.

**Figure 3:** the ±0.5 percentage-point band is the prespecified empirical cross-CDM reproducibility tolerance for risk difference, not a clinical noninferiority margin.

**Figure 4:** the 0.10 SMD line is a conventional descriptive reference value, not a prespecified statistical threshold. Fixed-cohort and end-to-end model results answer different questions.

**Extended Data Figure 1:** mapped semantic agreement and mapping/coverage limitations are shown separately so unresolved or unmapped records are not misclassified as mapped-event disagreement.

**Extended Data Figure 2:** panels a and b use restricted ranges to make near-exact agreement visible. Panel c retains the recurrent-stroke sensitivity as a secondary analysis and identifies PCORnet and OMOP explicitly.

**Extended Data Figure 3:** calibration slope and intercept are descriptive reproducibility results; no calibration-equivalence margin was prespecified.

## Publication tables

The current code-generated main and supplementary tables are produced by:

```bash
pcornet-omop-tables \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir results/publication_assets/tables
```

The table generator writes reader-facing CSV files and a JSON specification for Main Tables 1–3 and Supplementary Tables S1–S14.

## Do not create parallel figure pipelines

If a figure needs revision, modify `publication_figures.py`, regenerate the figure, and inspect the new output. Do **not** create `v2`, `v3`, `final`, `jamia`, or other parallel plotting modules/directories for incremental revisions. Git history already provides version provenance.
