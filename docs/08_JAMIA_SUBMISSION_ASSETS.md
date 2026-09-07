# JAMIA submission assets

This document describes the aggregate-only publication pipeline used for the JAMIA-oriented manuscript package.

## Scientific source of truth

Figures and tables read from the frozen aggregate study artifacts and locked study definitions. They do not require patient-level data.

The JAMIA-facing figure design emphasizes the manuscript's central contrast:

1. mapped semantic fidelity can be exact within prespecified mapped-event denominators;
2. independently constructed phenotype membership can still diverge because of an upstream ETL eligibility policy;
3. symmetric diagnosis-date eligibility restores exact phenotype membership/index dates;
4. fixed-patient/fixed-index downstream outcomes are exact;
5. independent end-to-end cohort construction changes the empirical population and final estimates.

## Main figures: current canonical pipeline

Use the reader-facing v3 builder for manuscript Figures 1-4:

```bash
python -m pcornet_omop_validation.study.publication_jamia_main_v3 \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia_v3
```

The v3 pipeline preserves the refined v2 quantitative layouts while replacing internal schema/code terminology in the reader-facing graphics with clinical and scientific terminology. Exact source identifiers remain in code and technical documentation rather than requiring readers to decode database field names in the figures.

The current main-figure jobs are:

- Figure 1: identify the reproducibility breakpoint, then contrast fixed-patient/index and independent end-to-end estimands;
- Figure 2: show source-faithful phenotype divergence, demonstrate exact rescue after applying the same diagnosis-date requirement, and localize the missing-diagnosis-date mechanism with a sparse fork diagram;
- Figure 3: contrast exact fixed-index outcome representation with end-to-end risk/population change and show the prespecified reproducibility tolerance directly;
- Figure 4: show end-to-end case-mix shift, discrimination change, and fixed-patient prediction agreement without overloading the panel set.

The earlier v2 builder is retained for provenance:

```bash
python -m pcornet_omop_validation.study.publication_jamia_main_v2 \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia_v2
```

## Extended Data figures

The existing JAMIA builders remain appropriate for supporting semantic-fidelity, association/prediction-agreement, recurrent-stroke, and calibration evidence.

```bash
python -m pcornet_omop_validation.study.publication_jamia_final \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia_extended
```

The earlier base builder remains available as:

```bash
python -m pcornet_omop_validation.study.publication_jamia_assets \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia
```

## Tables

```bash
python -m pcornet_omop_validation.study.publication_jamia_tables \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir tables/jamia
```

The script writes reader-facing CSV files and a JSON specification for main Tables 1-3 and Supplementary Tables S1-S14. Supplementary Table S14 records the locked D0/D1/D3 phenotype definitions from the versioned Stage C study definitions.

## Numerical display policy

Reader-facing precision is intentionally limited to the minimum needed to preserve meaningful distinctions. Exact computational values remain in the locked machine-readable study artifacts.

## Terminology policy

Main-manuscript figures use reader-facing clinical/scientific terms such as **diagnosis date**, **procedure date**, and **principal diagnosis** rather than raw table or field identifiers. Exact PCORnet/OMOP schema identifiers are preserved in the reproducible code and may be reported in technical supplementary material when needed for implementation reproducibility.

## Submission note

The manuscript treats the +/-0.5 percentage-point risk-difference margin and risk ratio 0.95-1.05 as prespecified **empirical cross-CDM reproducibility tolerances**, not clinical noninferiority margins or formal population-level equivalence tests.
