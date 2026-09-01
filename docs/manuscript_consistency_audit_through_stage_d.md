# Manuscript consistency audit through Stage D

_Last updated: 2026-09-01_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This audit compares `docs/publication_integrated_manuscript_draft_through_stage_d.md` against the locked/completion records for Stages A-D and the prespecified Stage D study definition. It distinguishes quantitative inconsistencies, protocol-completeness gaps, exploratory-definition ambiguities, and wording that should be constrained before journal submission.

## Overall verdict

The central Stage A-D quantitative results in the integrated manuscript are internally consistent with the locked completion records. The primary Stage D conclusion is supported: fixed-index 30-day and 90-day acute-care outcomes were reproduced exactly and met the prespecified equivalence margins, while end-to-end risk equivalence was not met when independently selected source and lineage-faithful OMOP cohorts were compared.

Two issues require resolution before the manuscript should be called final.

## Issue 1: prespecified median time-to-event metric was not reported

The locked Stage D study definition lists the following under the secondary time-to-event estimand:

- median days to event among event-positive patients;
- exact first-event-date agreement among patients positive in both representations;
- within-1-day first-event-date agreement.

The completed Stage D JSON reports the exact and within-one-day agreement counts, but it does not report the prespecified median days to first event. The analysis code computed serial-date averages internally but did not write a median time-to-event metric to the final result.

This is a protocol-completeness gap, not evidence against the primary equivalence result. The missing metric should be generated under the already locked fixed-index 90-day cohort and event definition before final manuscript freeze.

A targeted aggregate-only supplement has been added:

`python -m pcornet_omop_validation.study.stage_d_stroke_completion_supplement --config config/etl_A.yaml`

It must reproduce the locked fixed-index 90-day eligible count, event counts, and exact first-event-date agreement before reporting the missing medians.

## Issue 2: exploratory recurrent-stroke PDX ambiguity

The locked D0 index phenotype explicitly requires `PDX=P`. The Stage D study definition describes the exploratory recurrent endpoint as a subsequent acute-care encounter/visit satisfying the locked ischemic-stroke diagnosis definition. That wording can reasonably be read as carrying forward the `PDX=P` requirement.

However, the implemented exploratory recurrent-stroke query uses the locked stroke ICD code set and acute-care encounter/visit criteria without filtering recurrent source diagnoses to `PDX=P`. The lineage-faithful OMOP recurrent query likewise does not require the source recurrent diagnosis to have `PDX=P`.

The completed exploratory result therefore corresponds to an acute-care recurrent stroke-code diagnosis endpoint, not an unambiguous full reuse of all D0 diagnosis semantics. Because this issue was identified after observing the exploratory result, the implemented endpoint must not be silently redefined retrospectively.

The appropriate handling is:

1. retain the completed exploratory result as the implemented analysis;
2. document the discrepancy transparently as an exploratory protocol ambiguity/deviation;
3. report a post-outcome `PDX=P` sensitivity separately;
4. do not use the sensitivity to alter the prespecified primary/secondary Stage D conclusions.

The same Stage D completion supplement generates this aggregate-only `PDX=P` sensitivity.

## Quantitative checks passed

### Stage A

The integrated manuscript correctly reports:

- frozen OMOP target counts;
- DIAGNOSIS source rows `11,484,577` and missing-`DX_DATE` exclusions `3,459,785`;
- PROCEDURES source rows `11,244,947` and missing-`PX_DATE` exclusions `16,924`;
- Drug concept-zero routes `17,469,480`.

### Stage B

The manuscript summary is consistent with the locked cross-wave results. Every mapped source semantic route in the prespecified Stage B denominators was found in native OMOP. The claim of zero unexplained residual numeric discordance is supported by the VITAL expression diagnostic. The manuscript appropriately separates mapped agreement from vocabulary/unit/value coverage.

### Stage C

The integrated manuscript correctly reports D0, D1, and D3 cohort counts, Jaccard values after rounding, and exact shared-index-date percentages. The explanatory diagnosis-date mechanism is correctly labeled post-outcome and does not redefine the locked phenotype.

### Stage D primary and secondary risk estimands

The manuscript correctly reports:

- fixed-index 90-day: `3,822` eligible, `1,132` events in both representations, risk `29.6180%`, absolute difference `0`, risk ratio `1.0`, both margins met;
- fixed-index 30-day: `4,374` eligible, `753` events in both representations, risk `17.2154%`, absolute difference `0`, risk ratio `1.0`, both margins met;
- end-to-end 90-day: source `6,508/1,798`, OMOP `3,822/1,132`, absolute difference `+1.9905` percentage points, risk ratio `1.0720`, equivalence not met;
- end-to-end 30-day: source `7,277/1,178`, OMOP `4,374/753`, absolute difference `+1.0274` percentage points, risk ratio `1.0635`, equivalence not met.

The first-event-date statement is also consistent with the completed Stage D artifact: all `1,132` both-positive patients had exact date agreement and within-one-day agreement.

### Recurrent-stroke implemented endpoint

The manuscript correctly reports the implemented exploratory result: `2,531` eligible, `263` source events, `258` OMOP events, `2,526` agreements, `5` source-only positives, and `0` OMOP-only positives. The post-outcome mechanism diagnostic correctly localizes all five patient-level discordances to absent diagnosis-to-condition lineage while visit lineage and timing remain preserved.

## Wording constraints before submission

The following claims are supported and should be retained:

- exact fixed-index 30-day and 90-day acute-care outcome fidelity under the locked Stage D definitions;
- failure of end-to-end equivalence in this dataset and ETL implementation;
- upstream D0 cohort attrition as the established major mechanism differentiating the end-to-end populations;
- mapped semantic preservation and phenotype reproducibility as distinct validation layers.

The following broader claims should be avoided:

- PCORnet and OMOP are globally equivalent;
- OMOP intrinsically changes risk estimates;
- the common data model alone caused the end-to-end difference;
- all downstream analyses will be equivalent when the index cohort is fixed;
- the exploratory recurrent endpoint fully reproduced every D0 stroke-diagnosis semantic unless the `PDX=P` ambiguity is explicitly addressed.

## Manuscript status after this audit

Stages A-C remain closed. The primary and secondary Stage D risk-equivalence results remain closed and unchanged. Final Stage D manuscript closure should wait for the targeted completion supplement because one prespecified time-to-event metric is missing and the exploratory recurrent endpoint needs a transparent `PDX=P` sensitivity/clarification.
