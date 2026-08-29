# Stage C ischemic-stroke completion record

_Last updated: 2026-08-29_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Stage C ischemic-stroke phenotype validation is analytically complete for D0, D1, and D3.

## Locked primary results

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | Source only | OMOP only | Patient Jaccard | Exact shared index date |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 9,815 | 6,001 | 6,001 | 3,814 | 0 | 0.611411 | 100.00% |
| D1 | 8,624 | 5,246 | 5,245 | 3,379 | 1 | 0.608116 | 97.39% |
| D3 | 7,565 | 4,710 | 4,709 | 2,856 | 1 | 0.622390 | 97.52% |

The D1/D3 completed primary concordance was produced under analysis SHA `96131db8e47ba89a909438d6208502c6c7cdbea3`. The analysis-only SQL index intervention was documented before the first completed D1/D3 outcome query and did not modify the frozen ETL or phenotype semantics.

## Post-outcome explanatory findings

The post-outcome source-only mechanism audit reproduced the completed D1/D3 cohort counts. All 3,379 D1 source-only patients and all 2,856 D3 source-only patients had null selected `DX_DATE` and lacked diagnosis lineage under the frozen required-date ETL policy. No source-only patient was attributable to a missing visit xwalk or loss of a condition row after an existing diagnosis xwalk.

The post-outcome index-date selection audit reproduced the completed concordance and showed that all 137 D1 and 117 D3 shared patients with nonmatching selected index dates had a different qualifying episode selected because the source-selected stroke diagnosis had null `DX_DATE` and lacked OMOP diagnosis lineage.

These audits are explanatory only. They were designed after observing the primary D1/D3 concordance and must not be described as prespecified confirmatory analyses.

## Scientific interpretation

Stage C demonstrates that complete phenotype reproducibility can be materially lower than mapped event-level semantic concordance even when downstream imaging and laboratory semantics are well preserved. In this analysis, the dominant loss mechanism was an upstream required-date rule for diagnosis materialization, not progressive failure of CT/MRI or lipid transformation.

## Native OMOP portability

Native-OMOP portability remains a secondary sensitivity because PCORnet `PDX` is not natively represented in OMOP core and 22 of the 214 locked lipid LOINCs lack active Standard Measurement/Observation targets in the frozen vocabulary. Native patient Jaccard values were 0.565 for D0, 0.583 for D1, and 0.601 for D3.

## Disclosure

All committed Stage C manuscript-oriented outputs are aggregate only. No patient identifiers, source-record identifiers, or row-level protected health information are committed.

## Transition to Stage D

Stage C should now be treated as closed for routine analysis. Do not modify the frozen ETL or the locked D0/D1/D3 phenotype definitions based on these results. Further Stage C querying should be limited to documented reviewer-driven sensitivity analyses or independently demonstrated defects.

The next analytical stage is Stage D: prespecified downstream analytical-equivalence testing. Stage D must freeze its estimands, cohorts, outcomes, models, effect measures, covariates, tolerances/decision criteria if any, and handling of source-only versus shared cohorts before examining the corresponding estimates.
