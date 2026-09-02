# Current status and publication plan

_Last updated: 2026-09-02_

For the historical methodological record, see `docs/project_history_and_decisions.md`. The publication ETL remains frozen at:

`887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Publication analyses are maintained on `publication/analysis`. The frozen ETL was not retuned in response to downstream concordance results.

## Current analytical status

Stages A-D are analytically closed. A post-freeze harmonized Stage C sensitivity and prespecified Stage E statistical/model reproducibility extension are also complete.

| Layer | Question | Status | Main conclusion |
| --- | --- | --- | --- |
| Stage A | Did the ETL follow explicit structural transformation rules? | Complete | Structural differences were explainable by eligibility, routing, vocabulary coverage, one-to-many expansion, and concept-zero policies. |
| Stage B | Were mapped patient-level clinical semantics preserved? | Complete/locked | Mapped semantics were preserved exactly in the prespecified denominators; remaining limitations were coverage/provenance issues rather than unexplained mapped-event loss. |
| Stage C | Were complete ischemic-stroke phenotypes reproducible? | Complete/closed | Source-faithful D0/D1/D3 reproducibility was reduced primarily by the frozen non-null-`DX_DATE` ETL requirement. |
| Stage C harmonized sensitivity | What happens when non-null `DX_DATE` eligibility is imposed symmetrically? | Complete, post-freeze sensitivity | D0, D1, and D3 became perfectly concordant: identical patients and identical index dates. |
| Stage D | Were downstream acute-care outcomes analytically equivalent? | Complete/closed | Fixed-index 30/90-day outcomes were exactly reproduced; end-to-end equivalence failed because upstream cohort attrition changed the analyzed population. |
| Stage E | Were descriptive features, association estimates, and prediction models reproducible? | Complete extension | On the same fixed cohort, feature distributions, logistic associations, and predictions were essentially identical; end-to-end differences appeared when the cohorts differed. |

## Frozen OMOP target counts

| OMOP table | Rows |
| --- | ---: |
| person | 27,089 |
| observation_period | 27,087 |
| visit_occurrence | 1,510,957 |
| condition_occurrence | 7,315,572 |
| procedure_occurrence | 4,182,803 |
| measurement | 85,715,435 |
| observation | 7,319,081 |
| drug_exposure | 48,458,058 |
| device_exposure | 196,660 |
| specimen | 93 |
| death | 6,955 |

These counts are recorded outcomes, not acceptance thresholds.

## Stage A: structural transformation

Major explicit exclusions were 3,459,785 DIAGNOSIS rows missing `DX_DATE` and 16,924 PROCEDURES rows missing `PX_DATE`. One-to-many Standard mappings, cross-domain routing, and concept-zero retention were preserved rather than forced into one-to-one row correspondence. Drug mapping coverage remained incomplete, with 17,469,480 concept-zero Drug routes.

## Stage B: mapped semantic preservation

Encounter and Death were exactly concordant. Every mapped source Condition, Procedure, Drug, and Measurement/Observation semantic route in the locked mapped denominators was present in OMOP. Target-side Condition and Procedure excesses were fully explained by other audited source provenance. Resolved UCUM units and mapped categorical values agreed exactly. Of 75,769,622 directly comparable numeric rows, 75,644,000 were directly exact; the remaining 125,622 differences were all VITAL rows fully explained by the frozen SQL expression, leaving zero unexplained numeric mismatches.

## Stage C: phenotype reproducibility

Primary source-faithful results:

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | Source-only | OMOP-only | Jaccard | Exact index date among shared |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 9,815 | 6,001 | 6,001 | 3,814 | 0 | 0.611 | 100.00% |
| D1 | 8,624 | 5,246 | 5,245 | 3,379 | 1 | 0.608 | 97.39% |
| D3 | 7,565 | 4,710 | 4,709 | 2,856 | 1 | 0.622 | 97.52% |

Mechanism audits showed that all D1/D3 source-only patients had a null selected `DX_DATE`, lacked diagnosis lineage under the frozen required-date ETL policy, and experienced no further loss after existing diagnosis lineage. Residual shared-patient index-date mismatches were explained by selection of another qualifying episode after the source-selected diagnosis was absent from OMOP.

### Harmonized non-null-`DX_DATE` sensitivity

This post-freeze sensitivity imposed the same non-null-`DX_DATE` requirement on the source phenotype before comparison.

| Phenotype | PCORnet | OMOP | Shared | Source-only | OMOP-only | Jaccard | Exact index-date agreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 6,198 | 6,198 | 6,198 | 0 | 0 | 1.000 | 100.00% |
| D1 | 5,246 | 5,246 | 5,246 | 0 | 0 | 1.000 | 100.00% |
| D3 | 4,710 | 4,710 | 4,710 | 0 | 0 | 1.000 | 100.00% |

The primary Stage C analysis remains unchanged. The harmonized sensitivity demonstrates that the observed source-faithful discordance was attributable to the asymmetric diagnosis-date eligibility interaction rather than an inability of OMOP to represent the qualifying phenotype once the same diagnosis eligibility was enforced.

## Stage D: analytical equivalence

### Fixed-index acute-care outcomes

| Estimand | Eligible | PCORnet events | OMOP events | PCORnet risk | OMOP risk | Absolute difference, pp | OMOP/source RR | Equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 90 day | 3,822 | 1,132 | 1,132 | 29.618% | 29.618% | 0.000 | 1.000 | Met |
| 30 day | 4,374 | 753 | 753 | 17.215% | 17.215% | 0.000 | 1.000 | Met |

For the 90-day endpoint, all 1,132 both-positive patients had the same first-event date and the median time to first event was 26.0 days in both representations.

### End-to-end acute-care outcomes

| Estimand | PCORnet eligible/events/risk | OMOP eligible/events/risk | Absolute difference, pp | OMOP/source RR | Equivalence |
| --- | --- | --- | ---: | ---: | --- |
| 90 day | 6,508 / 1,798 / 27.628% | 3,822 / 1,132 / 29.618% | +1.990 | 1.072 | Not met |
| 30 day | 7,277 / 1,178 / 16.188% | 4,374 / 753 / 17.215% | +1.027 | 1.063 | Not met |

The contrast between exact fixed-index equivalence and failed end-to-end equivalence localizes the analytical divergence upstream to cohort construction rather than post-index acute-care event transformation.

### Exploratory recurrent stroke

The implemented exploratory recurrent stroke-code endpoint, which did not require recurrent `PDX=P`, included 2,531 fixed-index patients: PCORnet identified 263 events and OMOP 258, with 2,526/2,531 label agreement (99.80%), five source-only positives, and no OMOP-only positives. All five discordances retained encounter/visit lineage and correct timing but lacked qualifying diagnosis-to-condition lineage. A post-outcome recurrent `PDX=P` sensitivity yielded 170 events in each representation with complete 2,531/2,531 label agreement.

## Stage E: statistical and prediction-model reproducibility

Stage E was prespecified before outcome/model queries and reused the locked Stage C/D0 and Stage D 90-day endpoint. The first execution stopped before model fitting because an anchor assertion detected a cohort-reconstruction implementation error. A documented compatibility correction restored exact Stage C/D anchors without changing the prespecified Stage E features, outcome, models, or metrics.

Locked anchors reproduced exactly: source D0 9,815; OMOP D0 6,001; source 90-day eligible/events 6,508/1,798; OMOP 3,822/1,132; fixed cohort 3,822 with 1,132 events in each representation.

### Fixed-cohort features

Among the same 3,822 patients, age, sex, index length of stay, prior acute-care count, and prior ischemic-stroke indicator were exactly concordant for all patients. Prior all-encounter count was exact for 3,806/3,822 patients; mean absolute difference was 0.00497 encounters and Spearman correlation was 0.999989. All fixed-cohort standardized mean differences were effectively zero.

### Fixed-cohort association and prediction models

Multivariable logistic-regression coefficients and odds ratios were nearly identical across PCORnet and OMOP. For example, OMOP/source odds-ratio ratios were 1.000015 for age, 1.000019 for female sex, 0.999949 for length of stay, 1.000352 for prior acute-care count, 0.999409 for prior all-encounter count, and 1.000241 for prior ischemic stroke.

| Fixed-cohort model | PCORnet AUROC | OMOP AUROC | AUROC difference | PCORnet AUPRC | OMOP AUPRC | Probability agreement |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Logistic regression | 0.591413 | 0.591345 | -0.000068 | 0.447941 | 0.447823 | Pearson 0.999994; mean absolute probability difference 0.000084 |
| Ridge logistic | 0.591509 | 0.591427 | -0.000082 | 0.447921 | 0.447822 | Pearson 0.999994; mean absolute probability difference 0.000084 |
| Histogram gradient boosting | 0.598737 | 0.597730 | -0.001007 | 0.439228 | 0.434959 | Pearson 0.965218; mean absolute probability difference 0.03698 |

The nonlinear model therefore showed more patient-level prediction sensitivity to small feature differences even though aggregate fixed-cohort performance remained close.

### End-to-end model comparison

The independently selected source and OMOP populations differed in several baseline distributions. The largest locked-feature standardized mean differences were -0.132 for prior 365-day acute-care utilization and -0.158 for prior ischemic-stroke history.

| End-to-end model | PCORnet AUROC | OMOP AUROC | Difference | PCORnet AUPRC | OMOP AUPRC | PCORnet Brier | OMOP Brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic regression | 0.634628 | 0.591345 | -0.043284 | 0.454775 | 0.447823 | 0.195089 | 0.206229 |
| Ridge logistic | 0.634705 | 0.591427 | -0.043278 | 0.454817 | 0.447822 | 0.195084 | 0.206210 |
| Histogram gradient boosting | 0.626769 | 0.597730 | -0.029040 | 0.437420 | 0.434959 | 0.200138 | 0.218271 |

These end-to-end differences combine upstream cohort selection with representation-specific feature construction and should not be interpreted as an intrinsic OMOP model-performance effect.

## Current scientific interpretation

The evidence supports a layered conclusion rather than a global claim of equivalence or nonequivalence:

1. The audited ETL preserved mapped clinical semantics with essentially exact fidelity in the locked mapped denominators.
2. A source phenotype that used encounter-date fallback selected many diagnoses the frozen ETL explicitly excluded for missing `DX_DATE`; this produced large source-faithful cohort differences.
3. When diagnosis-date eligibility was harmonized, D0/D1/D3 phenotype membership and index dates were exactly reproduced.
4. When patient and index date were held fixed, downstream acute-care outcomes were exact and conventional logistic-model features, associations, and predictions were essentially identical.
5. When the complete source and OMOP cohorts were allowed to differ, downstream risks, baseline feature distributions, and prediction-model performance also differed.

The strongest manuscript-level message is therefore: **an audited PCORnet-to-OMOP conversion can preserve mapped clinical information and conditional analyses extremely well while an explicit upstream eligibility policy can still alter cohort membership and thereby change end-to-end scientific results.**

## Reproducibility and disclosure status

- Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.
- Stage E study definition was frozen before outcome/model fitting.
- Generated `results/` are intentionally excluded from Git by `.gitignore`.
- Publication-facing Git records therefore consist of code, locked study definitions, aggregate completion records, manuscript summaries, and disclosure-reviewed result summaries.
- Patient identifiers, row-level predictions, and row-level PHI are not committed.
- The local Stage E completed JSON reported a clean worktree and passed aggregate-only disclosure review.

## Immediate next steps

1. Treat Stages A-E as scientifically complete for routine publication work; do not add exploratory analyses unless needed for a specific scientific question or reviewer request.
2. Use `docs/publication_integrated_manuscript_draft_through_stage_d.md` together with `docs/publication_stage_e_manuscript_extension.md` as the current manuscript source until a journal-targeted consolidated draft is created.
3. Use `docs/publication_results_dashboard_through_stage_e.md` as the human-readable quantitative reference for manuscript writing and table/figure construction.
4. Select a primary and backup journal, verify current author instructions, and then produce the journal-targeted manuscript and submission package.
5. Preserve the distinction between prespecified analyses, post-freeze sensitivity analyses, and post-outcome explanatory diagnostics in all manuscript language.
