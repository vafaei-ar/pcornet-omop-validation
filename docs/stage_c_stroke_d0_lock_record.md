# Stage C ischemic-stroke D0 analytical lock record

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked D0 definition: `study_definitions/stage_c_stroke_d0_v1.json`

Status: **analytically complete**

## Completion criteria

The final D0 manuscript/invariant bundle completed successfully with:

- `all_invariants_matched: true`
- `disclosure_review.status: passed`
- matched unique PATID-to-person bridge
- preflight confirmed no outcome query was performed before phenotype lock
- primary PCORnet/OMOP cohort partitions closed exactly
- zero OMOP-only patients in the primary transformation-fidelity estimand
- all shared primary patients had exact index-date agreement
- all primary source-only patients were fully classified by the prespecified discordance categories
- all 3,814 source-only patients were explained by required diagnosis-date missingness / frozen ETL exclusion
- native-OMOP portability sensitivity remained separate from the primary estimand

## Primary transformation-fidelity D0

| Metric | Result |
| --- | ---: |
| PCORnet D0 patients | 9,815 |
| Lineage-faithful OMOP D0 patients | 6,001 |
| Shared patients | 6,001 |
| PCORnet-only patients | 3,814 |
| OMOP-only patients | 0 |
| Patient Jaccard | 0.6114111055 |
| Positive agreement | 75.8852% |
| Exact index-date agreement among shared | 100.0% |

All 6,001 OMOP patients were present in the locked source cohort and had exactly matching index dates.

## Primary discordance decomposition

All 3,814 PCORnet-only patients were classified as:

`required_source_date_missing_or_etl_excluded`

The locked source phenotype permits index-date fallback to encounter dates when `DX_DATE` is missing. The frozen ETL, by design, excludes diagnosis records missing the required diagnosis date. Therefore this attrition is a prespecified phenotype/ETL policy interaction rather than unexplained transformed-event loss.

No ETL rule or phenotype rule was changed in response to this result.

## Native-OMOP portability sensitivity

This secondary sensitivity intentionally omits source `PDX == 'P'` because no native OMOP core equivalent exists in the frozen representation.

| Metric | Result |
| --- | ---: |
| PCORnet reference patients | 9,815 |
| Native-OMOP portable patients | 7,667 |
| Shared patients | 6,312 |
| PCORnet-only patients | 3,503 |
| Native-OMOP-only patients | 1,355 |
| Patient Jaccard | 0.5650850492 |
| Positive agreement | 72.2114% |

This sensitivity is a portability/representability result and does not replace the primary transformation-fidelity estimand.

## Disclosure review

The final D0 manuscript bundle is aggregate-only and records:

- no patient identifiers written
- no source-record identifiers written
- no row-level PHI written
- no free-text clinical values written

## Freeze policy for downstream work

D0 Stage C is now locked. The D0 phenotype definition, matching rules, discordance categories, and interpretation should not be changed based on observed agreement unless an independently demonstrated methodological defect is documented first.

D1 and D3 remain deferred until the external lipid LOINC whitelist is versioned or otherwise hashed as a reproducible study artifact and their imaging/lipid evidence windows are locked before outcome queries.

This record closes the first Stage C phenotype reproducibility analysis without modifying the frozen ETL.
