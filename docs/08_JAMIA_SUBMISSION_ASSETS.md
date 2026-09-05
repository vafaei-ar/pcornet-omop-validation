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

## Figures

Base builders:

```bash
python -m pcornet_omop_validation.study.publication_jamia_assets \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia
```

Final visually reviewed export wrapper:

```bash
python -m pcornet_omop_validation.study.publication_jamia_final \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir figures/jamia
```

The final wrapper preserves the aggregate-only base builders and applies the reviewed annotation positions needed to avoid label/title and label/data collisions in Figures 2–4.

Outputs are PNG, PDF and SVG for:

- Figure 1: reproducibility breakpoint / fixed-vs-end-to-end conceptual result
- Figure 2: phenotype divergence and diagnosis-date mechanism
- Figure 3: outcome reproducibility under fixed and end-to-end estimands
- Figure 4: population and prediction-model reproducibility
- Extended Data Figure 1: semantic fidelity and coverage limitations
- Extended Data Figure 2: additional analytical reproducibility
- Extended Data Figure 3: calibration reproducibility

## Tables

```bash
python -m pcornet_omop_validation.study.publication_jamia_tables \
  --data study_definitions/artifacts/publication_figure_data_v1.json \
  --outdir tables/jamia
```

The script writes reader-facing CSV files and a JSON specification for main Tables 1–3 and Supplementary Tables S1–S14. Supplementary Table S14 records the locked D0/D1/D3 phenotype definitions from the versioned Stage C study definitions.

## Numerical display policy

Reader-facing precision is intentionally limited to the minimum needed to preserve meaningful distinctions. Exact computational values remain in the locked machine-readable study artifacts.

## Submission note

The manuscript treats the ±0.5 percentage-point risk-difference margin and risk ratio 0.95–1.05 as prespecified **empirical cross-CDM reproducibility tolerances**, not clinical noninferiority margins or formal population-level equivalence tests.
