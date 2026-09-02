# Publication results dashboard through Stage E

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This document is a human-readable quantitative dashboard for manuscript writing. It summarizes the disclosure-reviewed aggregate results from Stages A-E and preserves the distinction between primary/prespecified analyses, post-freeze sensitivities, and post-outcome diagnostics.

## 1. Structural transformation: Stage A

### Frozen OMOP target counts

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

### Major explicit source exclusions and coverage limitations

| Domain/source family | Source / eligible information | Main result |
| --- | --- | --- |
| DIAGNOSIS | 11,484,577 source rows | 8,024,792 eligible; 3,459,785 excluded (30.125%), all for missing `DX_DATE` |
| PROCEDURES | 11,244,947 source rows | 11,228,023 eligible; 16,924 excluded (0.151%), missing `PX_DATE` |
| Procedure routes | 11,234,863 route-ledger components | 11,121,561 event routes; 111,660 unresolved; 1,642 non-event semantic components |
| DIAGNOSIS + CONDITION | 8,674,973 eligible source events | 9,045,157 canonical routes; 361,606 events generated >1 core route; 60,148 Condition concept-zero fallback |
| OBS_CLIN | 38,850,928 rows | 37,327,978 Measurement; 1,471,098 Observation; 39,115 Condition; 12,737 unresolved Observation concept 0 |
| Drugs | 48,457,880 routes | 30,988,400 mapped nonzero Standard Drug; 17,469,480 concept-zero (36.05%) |

Interpretation: raw row equality is not an appropriate universal validity criterion because eligible source events may expand one-to-many or route across OMOP domains.

## 2. Patient-level semantic preservation: Stage B

| Validation component | Locked result | Interpretation |
| --- | --- | --- |
| Encounter | 1,510,957 exact; patient Jaccard 1.000 | Exact |
| Death | 6,955 exact; patient Jaccard 1.000 | Exact |
| Condition mapped routes | 8,983,621 mapped source routes, all exact | No mapped-route loss |
| Condition target same semantic space | 9,739,734 | 756,113 excess fully explained by other audited provenance |
| Procedure mapped routes | 11,121,561, all exact | No mapped-route loss |
| Procedure target same semantic space | 12,659,204 | 1,537,643 excess attributable to other provenance |
| Drug mapped nonzero Standard Drug | 30,988,400, all exact | Patient Jaccard 1.000; 48 target extras explained |
| Measurement/Observation mapped rows | 92,668,145 exact | Zero unmatched; patient Jaccard 1.000 |
| Numeric values | 75,769,622 directly comparable | 75,644,000 directly exact; 125,622 VITAL differences fully explained by frozen SQL expansion; zero unexplained |
| Units | 58,916,347 uniquely resolved active Standard UCUM rows | 100% agreement |
| Categorical values | 809,630 mapped categorical concepts | 100% agreement |

Coverage caveats kept separate from mapped agreement: 60,148 Condition concept-zero fallback, 111,660 unresolved procedure components, 17,469,480 Drug concept-zero routes, and 366,371 unresolved/descriptive Measurement/Observation concept-zero rows.

## 3. Stroke phenotype reproducibility: Stage C

### Primary source-faithful comparison

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | Source-only | OMOP-only | Jaccard | Exact index date among shared |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 9,815 | 6,001 | 6,001 | 3,814 | 0 | 0.611411 | 100.00% |
| D1 | 8,624 | 5,246 | 5,245 | 3,379 | 1 | 0.608116 | 97.39% |
| D3 | 7,565 | 4,710 | 4,709 | 2,856 | 1 | 0.622390 | 97.52% |

Native-OMOP portable sensitivity Jaccards were 0.565 for D0, 0.583 for D1, and 0.601 for D3.

Mechanism: all D1/D3 source-only patients had null selected `DX_DATE`, lacked diagnosis lineage, and had no further loss after existing xwalk. Shared index-date mismatches were explained by selection of another qualifying episode because the source-selected diagnosis was absent from OMOP. Imaging/lipid transformation was not the dominant cause.

### Post-freeze harmonized non-null-`DX_DATE` sensitivity

| Phenotype | PCORnet | OMOP | Shared | Source-only | OMOP-only | Jaccard | Exact index-date agreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 6,198 | 6,198 | 6,198 | 0 | 0 | 1.000 | 100.00% |
| D1 | 5,246 | 5,246 | 5,246 | 0 | 0 | 1.000 | 100.00% |
| D3 | 4,710 | 4,710 | 4,710 | 0 | 0 | 1.000 | 100.00% |

Interpretation: once the diagnosis-date eligibility rule was imposed symmetrically, residual D0/D1/D3 discordance disappeared. This does not replace the primary source-faithful comparison; it isolates the effect of asymmetric date eligibility.

## 4. Downstream analytical equivalence: Stage D

### Fixed-index acute-care outcomes

| Estimand | Eligible | PCORnet events | OMOP events | PCORnet risk | OMOP risk | Absolute difference, pp | OMOP/source RR | Prespecified equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 90 day | 3,822 | 1,132 | 1,132 | 29.6180% | 29.6180% | 0.0000 | 1.0000 | Met |
| 30 day | 4,374 | 753 | 753 | 17.2154% | 17.2154% | 0.0000 | 1.0000 | Met |

Additional 90-day time-to-event result: all 1,132 both-positive first-event dates were exact; median time to first event was 26.0 days in both representations.

### End-to-end acute-care outcomes

| Estimand | PCORnet eligible | PCORnet events | PCORnet risk | OMOP eligible | OMOP events | OMOP risk | Absolute difference, pp | OMOP/source RR | Equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 90 day | 6,508 | 1,798 | 27.6275% | 3,822 | 1,132 | 29.6180% | +1.9905 | 1.0720 | Not met |
| 30 day | 7,277 | 1,178 | 16.1880% | 4,374 | 753 | 17.2154% | +1.0274 | 1.0635 | Not met |

Interpretation: when patient/index are held fixed, the acute-care outcome is exactly reproduced. End-to-end estimates differ because the upstream phenotype changes the analyzed population.

### Exploratory recurrent ischemic stroke

| Analysis | Eligible | PCORnet events | OMOP events | Agreement | Source-only | OMOP-only |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Implemented recurrent stroke-code endpoint, days 31-365, no recurrent `PDX=P` filter | 2,531 | 263 | 258 | 2,526/2,531 (99.80%) | 5 | 0 |
| Post-outcome recurrent `PDX=P` sensitivity | 2,531 | 170 | 170 | 2,531/2,531 (100%) | 0 | 0 |

The five original recurrent discordances retained correct visit lineage and timing but lacked qualifying diagnosis-to-condition lineage.

## 5. Statistical and prediction-model reproducibility: Stage E

Stage E prespecified six core features and three prediction models before outcome/model queries. The completed run reproduced all Stage C/D anchors exactly before feature construction or model fitting.

### Fixed-cohort feature reproducibility, n=3,822

| Feature | Main comparison |
| --- | --- |
| Age at index | Exact for 3,822/3,822; SMD 0 |
| Female indicator | Exact for 3,822/3,822; SMD 0 |
| Index length of stay | Exact for 3,822/3,822; SMD 0 |
| Prior 365-day acute-care encounter count | Exact for 3,822/3,822; SMD 0 |
| Prior 365-day all-encounter count | Exact for 3,806/3,822; mean absolute difference 0.00497; Spearman 0.999989; SMD 0.000391 |
| Prior 365-day ischemic-stroke indicator | Exact for 3,822/3,822; SMD 0 |

### Fixed-cohort multivariable logistic association reproducibility

| Predictor | OMOP/source odds-ratio ratio |
| --- | ---: |
| Age at index | 1.000015 |
| Female indicator | 1.000019 |
| Index length of stay | 0.999949 |
| Prior acute-care encounters | 1.000352 |
| Prior all encounters | 0.999409 |
| Prior ischemic stroke | 1.000241 |

These values are essentially 1.0, indicating nearly identical adjusted associations on the same patients.

### Fixed-cohort prediction performance

The deterministic test set contained 1,140 patients with 358 events in both representations.

| Model | PCORnet AUROC | OMOP AUROC | AUROC diff | PCORnet AUPRC | OMOP AUPRC | Brier PCORnet | Brier OMOP | Patient-level probability agreement |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Logistic regression | 0.591413 | 0.591345 | -0.000068 | 0.447941 | 0.447823 | 0.206220 | 0.206229 | Pearson 0.999994; Spearman 0.999997; mean abs diff 0.000084 |
| Ridge logistic | 0.591509 | 0.591427 | -0.000082 | 0.447921 | 0.447822 | 0.206201 | 0.206210 | Pearson 0.999994; Spearman 0.999997; mean abs diff 0.000084 |
| Histogram gradient boosting | 0.598737 | 0.597730 | -0.001007 | 0.439228 | 0.434959 | 0.217289 | 0.218271 | Pearson 0.965218; Spearman 0.949380; mean abs diff 0.03698 |

Interpretation: conventional logistic predictions were effectively identical. The nonlinear model amplified the small feature discrepancies at the individual prediction level, although aggregate discrimination changed very little.

### End-to-end feature distribution differences

| Feature | PCORnet | OMOP | SMD source-minus-OMOP |
| --- | --- | --- | ---: |
| Age | mean 67.11 | mean 67.34 | -0.0159 |
| Female | 46.94% | 48.01% | -0.0214 |
| Length of stay | mean 6.91 d | mean 7.68 d | -0.0780 |
| Prior acute-care count | mean 0.748 | mean 0.963 | -0.1323 |
| Prior all-encounter count | mean 6.773 | mean 7.491 | -0.0583 |
| Prior ischemic stroke | 6.28% | 10.68% | -0.1581 |

The largest differences were prior acute-care utilization and prior ischemic-stroke history.

### End-to-end prediction performance

The independently selected cohorts were 6,508 source patients and 3,822 OMOP patients.

| Model | PCORnet AUROC | OMOP AUROC | Difference | PCORnet AUPRC | OMOP AUPRC | PCORnet Brier | OMOP Brier |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic regression | 0.634628 | 0.591345 | -0.043284 | 0.454775 | 0.447823 | 0.195089 | 0.206229 |
| Ridge logistic | 0.634705 | 0.591427 | -0.043278 | 0.454817 | 0.447822 | 0.195084 | 0.206210 |
| Histogram gradient boosting | 0.626769 | 0.597730 | -0.029040 | 0.437420 | 0.434959 | 0.200138 | 0.218271 |

These differences should be interpreted as end-to-end population/representation differences, not as an intrinsic loss of modeling capability in OMOP.

## 6. Overall scientific synthesis

| Layer held fixed / allowed to vary | Result |
| --- | --- |
| Structural ETL rules | Differences explainable by explicit policies and semantic routing |
| Mapped patient-level semantics | Essentially exact preservation |
| Source-faithful complete phenotype | Substantial discordance because source fallback and ETL date eligibility differ |
| Harmonized phenotype eligibility | Perfect D0/D1/D3 reproduction |
| Same patient + same index + same observability | Exact 30/90-day outcome reproduction |
| Same fixed cohort + independently constructed features | Nearly exact conventional feature, association, and logistic prediction reproducibility |
| Independently selected end-to-end cohorts | Risks, baseline distributions, and model performance differ |

### Main message

**An audited PCORnet-to-OMOP conversion can preserve mapped clinical information and conditional downstream analyses extremely well, while an explicit upstream eligibility policy can still alter cohort membership and thereby change end-to-end risks and prediction-model performance.**

## 7. Reporting guardrails

- Do not claim global PCORnet/OMOP equivalence.
- Do not claim the original Stage C comparison was unfair or invalid; it answers the practical source-faithful portability question. The harmonized sensitivity answers a different representation-isolation question.
- Do not describe end-to-end differences as an intrinsic OMOP CDM effect.
- Preserve the distinction between prespecified analyses, post-freeze sensitivities, and post-outcome diagnostics.
- Do not reinterpret the recurrent `PDX=P` sensitivity as the originally implemented recurrent endpoint.
- Keep concept-zero and unresolved vocabulary coverage separate from mapped semantic concordance.
