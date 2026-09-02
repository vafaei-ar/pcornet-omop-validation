# Stage E first-run anchor reconstruction correction

_Date: 2026-09-02_

The first execution attempt of `stage_e_statistical_model_reproducibility` stopped before descriptive feature analysis or model fitting because the locked Stage C/D anchor check failed.

Observed before the stop:

- source D0: 9,815 (correct)
- OMOP D0: 6,198 (expected 6,001)
- source 90-day eligible/events: 6,508 / 1,798 (correct)
- OMOP 90-day eligible/events: 3,967 / 1,167 (expected 3,822 / 1,132)
- fixed shared 90-day eligible/events: 3,822 / 1,132 in both representations (correct)

The cause was an analysis-code reconstruction error, not a change in data or study definition. The first Stage E implementation selected the earliest surviving OMOP-materialized D0 episode per patient from all source D0 candidate episodes. The locked Stage C lineage-faithful estimand instead first selects the patient's source D0 index episode and then evaluates whether that exact selected episode materialized in OMOP. Selecting after OMOP materialization can substitute a later surviving episode for a source-selected episode that was excluded by the ETL, producing the harmonized-like 6,198 cohort rather than the locked 6,001 cohort.

A compatibility wrapper, `stage_e_statistical_model_reproducibility_anchorfix.py`, corrects only this reconstruction detail by anchoring OMOP materialization to the already selected `#src_d0` episode and not reapplying target-side age selection. The Stage E study-definition hash, outcome, features, train/test split, models, and evaluation metrics are unchanged.

Because the failed run terminated at the anchor assertion, no Stage E descriptive results, regression estimates, fitted prediction models, or model-performance results were produced or inspected before this correction.
