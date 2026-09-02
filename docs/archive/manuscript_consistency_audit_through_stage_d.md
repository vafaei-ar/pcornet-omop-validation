# Manuscript consistency audit through Stage D

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This audit compares `docs/publication_integrated_manuscript_draft_through_stage_d.md` against the locked/completion records for Stages A-D and the prespecified Stage D study definition. It distinguishes quantitative inconsistencies, protocol-completeness gaps, exploratory-definition ambiguities, and wording that should be constrained before journal submission.

## Overall verdict

The central Stage A-D quantitative results in the integrated manuscript are internally consistent with the locked completion records. The primary Stage D conclusion is supported: fixed-index 30-day and 90-day acute-care outcomes were reproduced exactly and met the prespecified equivalence margins, while end-to-end risk equivalence was not met when independently selected source and lineage-faithful OMOP cohorts were compared.

The two issues identified during this audit have now been resolved transparently without changing the frozen primary or secondary Stage D estimands.

## Resolved issue 1: prespecified median time-to-event metric

The locked Stage D study definition prespecified median days to event among event-positive patients in addition to exact and within-one-day first-event-date agreement. The original completed Stage D JSON omitted that median metric.

The aggregate-only completion supplement reproduced the locked fixed-index 90-day denominator and event counts before reporting the missing metric:

- eligible: `3,822`;
- source event-positive: `1,132`;
- OMOP event-positive: `1,132`;
- exact first-event-date agreement: `1,132`;
- source median days to first event: `26.0`;
- OMOP median days to first event: `26.0`.

This resolves the protocol-completeness gap. No cohort, event, observability, or equivalence definition changed.

## Resolved issue 2: exploratory recurrent-stroke `PDX` ambiguity

The implemented exploratory recurrent-stroke query used the locked stroke ICD code set and acute-care criteria without applying recurrent-diagnosis `PDX=P`. The original implemented result remains unchanged and is now described precisely as that implemented endpoint:

- eligible: `2,531`;
- source events: `263`;
- OMOP events: `258`;
- label agreement: `2,526`;
- source-only positives: `5`;
- OMOP-only positives: `0`.

A post-outcome `PDX=P` sensitivity was run separately because the Stage D wording could reasonably be read as carrying forward the D0 primary-diagnosis criterion. Under that sensitivity:

- eligible: `2,531`;
- source events: `170`;
- OMOP events: `170`;
- label agreement: `2,531`;
- source-only positives: `0`;
- OMOP-only positives: `0`.

The sensitivity is perfectly concordant but must remain labeled post-outcome and must not replace the originally implemented recurrent endpoint.

## Quantitative checks passed

### Stage A

The integrated manuscript correctly reports frozen OMOP target counts, DIAGNOSIS source rows `11,484,577` and missing-`DX_DATE` exclusions `3,459,785`, PROCEDURES source rows `11,244,947` and missing-`PX_DATE` exclusions `16,924`, and Drug concept-zero routes `17,469,480`.

### Stage B

The manuscript summary is consistent with the locked cross-wave results. Every mapped source semantic route in the prespecified Stage B denominators was found in native OMOP. The claim of zero unexplained residual numeric discordance is supported by the VITAL expression diagnostic. The manuscript appropriately separates mapped agreement from vocabulary/unit/value coverage.

### Stage C

The integrated manuscript correctly reports D0, D1, and D3 cohort counts, Jaccard values after rounding, and exact shared-index-date percentages. The explanatory diagnosis-date mechanism is correctly labeled post-outcome and does not redefine the locked phenotype.

### Stage D primary and secondary risk estimands

The manuscript correctly reports:

- fixed-index 90-day: `3,822` eligible, `1,132` events in both representations, risk `29.6180%`, absolute difference `0`, risk ratio `1.0`, both margins met;
- fixed-index 30-day: `4,374` eligible, `753` events in both representations, risk `17.2154%`, absolute difference `0`, risk ratio `1.0`, both margins met;
- end-to-end 90-day: source `6,508/1,798`, OMOP `3,822/1,132`, absolute difference `+1.9905` percentage points, risk ratio `1.0720`, equivalence not met;
- end-to-end 30-day: source `7,277/1,178`, OMOP `4,374/753`, absolute difference `+1.0274` percentage points, risk ratio `1.0635`, equivalence not met;
- median days to first 90-day event: `26.0` in both representations;
- exact first-event-date agreement: `1,132 / 1,132` both-positive patients.

## Wording constraints before submission

The following claims are supported and should be retained:

- exact fixed-index 30-day and 90-day acute-care outcome fidelity under the locked Stage D definitions;
- failure of end-to-end equivalence in this dataset and ETL implementation;
- upstream D0 cohort attrition as the established major mechanism differentiating the end-to-end populations;
- mapped semantic preservation and phenotype reproducibility as distinct validation layers;
- perfect concordance of the post-outcome recurrent `PDX=P` sensitivity, explicitly labeled as such.

The following broader claims should be avoided:

- PCORnet and OMOP are globally equivalent;
- OMOP intrinsically changes risk estimates;
- the common data model alone caused the end-to-end difference;
- all downstream analyses will be equivalent when the index cohort is fixed;
- the originally implemented recurrent endpoint included `PDX=P`.

## Final manuscript status after this audit

Stages A-D are closed for routine analysis. The integrated manuscript now includes the missing prespecified median time-to-event metric and transparently separates the implemented recurrent endpoint from the post-outcome `PDX=P` sensitivity. No unresolved quantitative inconsistency identified by this audit remains.

The next phase is manuscript finalization: freeze the main tables/figures, perform a final disclosure/reproducibility pass, then adapt the manuscript to the selected journal.
