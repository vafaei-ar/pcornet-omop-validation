# Stage B Wave 2 progress

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked Wave 2 definition: `study_definitions/stage_b_wave2_v1.json`

## Wave 2 preflight

The Wave 2 preflight completed successfully against the frozen build.

Key denominators:

- base Drug route rows: 48,457,880
- mapped nonzero Drug routes: 30,988,400
- unresolved Drug routes: 17,469,480
- Condition-derived cross-domain Drug rows: 178
- final Drug Exposure rows: 48,458,058
- LAB source rows with RESULT_DATE: 33,115,308 of 33,115,308
- VITAL numeric value rows: 11,673,550
- VITAL categorical value rows: 2,170,885
- OBS_CLIN routed domains: 3
- OBS_GEN source rows: 353,586
- final Measurement rows: 85,715,435
- final Observation rows: 7,319,081
- duplicate active Standard UCUM code groups under the case-sensitive rule: 0

## Drug semantic concordance

Drug semantic concordance completed under the locked person + calendar start date + Standard Drug concept multiset definition.

Primary result:

- distinct source Drug events represented in the route ledger: **47,511,808**
- source Drug route rows: **48,457,880**
- mapped nonzero Standard Drug route rows: **30,988,400**
- unresolved concept-zero Drug route rows: **17,469,480**
- source events with multiple mapped Standard Drug routes: **279,571**
- source mapped patients: **26,656**
- native OMOP patients in the same mapped Drug concept space: **26,656**
- patient Jaccard: **1.0**
- exact person/date/concept matched mapped events: **30,988,400**
- source unmatched mapped semantic events: **0**
- target unmatched rows in the same Drug concept space before attribution: **48**
- source mapped-event match percent: **100.0%**

Secondary provenance attribution classified the 48 target-side excess rows as other audited provenance. All 48 have Drug Type concept 0 and Route concept 0. The base Drug-derived mapped rows therefore reconcile exactly to the 30,988,400 mapped source Drug routes.

## Drug mapping coverage

The unresolved Drug population remains an important coverage result rather than a mapped-concordance failure. The largest unresolved components are missing or unresolved medication coding in PRESCRIBING and MED_ADMIN. No arbitrary concept assignment is introduced for those rows.

Mapped versus unresolved by source family:

| Source family | Route rows | Mapped rows | Unresolved rows |
|---|---:|---:|---:|
| DISPENSING | 8,370,247 | 8,167,230 | 203,017 |
| IMMUNIZATION | 21,132 | 4,311 | 16,821 |
| MED_ADMIN | 14,097,424 | 5,737,574 | 8,359,850 |
| PRESCRIBING | 24,257,671 | 15,397,097 | 8,860,574 |
| PROCEDURES | 1,711,406 | 1,682,188 | 29,218 |

Secondary target characterization among the mapped Drug concept space:

- Route concept nonzero: 18,008,980 rows
- Route concept zero: 12,979,468 rows
- Visit-linked: 14,613,728 rows
- Drug Type concept zero: 48 rows, all from other audited provenance

Route-concept zero is not treated as Drug semantic failure under the locked Wave 2 definition.

## Measurement / Observation preflight

The family-specific Measurement/Observation preflight completed successfully before event-level semantic comparison.

Source and lineage structure:

- LAB_RESULT_CM: 33,115,308 source rows; all have RESULT_DATE, nonblank LOINC, and nonblank unit text; 26,769,840 have numeric result values.
- LAB lineage: 33,053,350 rows to Measurement and 61,958 rows to Observation, accounting for all LAB rows.
- VITAL: 6,537,020 source rows, all with MEASURE_DATE; 11,673,550 expanded numeric Measurement values and 2,170,885 categorical Observation values.
- OBS_CLIN: 37,327,978 mapped Measurement routes; 1,471,098 mapped Observation routes plus 12,737 unresolved Observation concept-zero routes; 39,115 Condition routes are outside the M/O denominator.
- PROCEDURES: 3,491,072 mapped Measurement routes plus 4 unresolved; 1,836,895 mapped Observation routes plus 44 unresolved.
- CONDITION cross-domain: 169,481 Measurement rows and 1,411,878 Observation rows.
- OBS_GEN: 353,586 source-preserved Observation concept-zero rows; descriptive only and excluded from mapped semantic concordance.

Target characterization:

- Measurement rows: 85,715,435; concept-zero rows: 4; unit-concept-zero rows: 26,799,088.
- Observation rows: 7,319,081; concept-zero rows: 366,367.
- Active Standard UCUM duplicate code groups under the frozen case-sensitive rule: 0.

These are coverage and provenance denominators, not agreement thresholds. Concept-zero families remain separate, and target lineage is reserved for secondary attribution.

## Measurement / Observation semantic presence

The primary semantic-presence comparison completed successfully under the locked person + calendar date + target domain + active Standard concept multiset definition.

Primary result:

- mapped source semantic rows: **92,668,145**
- native target rows in the same source-derived concept space: **92,668,145**
- mapped source patients: **27,025**
- mapped target patients: **27,025**
- patient Jaccard: **1.0**
- exact person/date/domain/concept matched events: **92,668,145**
- source unmatched mapped semantic events: **0**
- target unmatched mapped semantic events: **0**
- source exact-signature match percent: **100.0%**

By target domain:

| Target domain | Source mapped rows | Target rows in source concept space | Exact matched | Source unmatched | Target unmatched |
|---|---:|---:|---:|---:|---:|
| Measurement | 85,715,431 | 85,715,431 | 85,715,431 | 0 | 0 |
| Observation | 6,952,714 | 6,952,714 | 6,952,714 | 0 | 0 |

The four Measurement concept-zero Procedure rows are excluded from the mapped semantic denominator. Observation unresolved/descriptive coverage is also separated: 12,737 OBS_CLIN concept-zero rows, 44 Procedure concept-zero rows, and 353,586 OBS_GEN descriptive concept-zero rows. LAB has no unresolved mapped-concept rows in this frozen build.

Secondary provenance attribution is complete for the entire mapped M/O concept space: all 92,668,145 target rows are attributed to CONDITION, LAB_RESULT_CM, OBS_CLIN, PROCEDURES, or VITAL, with **0 unattributed rows**.

## Measurement / Observation value layers

The locked secondary value/unit analysis completed.

Numeric values:

- directly comparable rows: **75,769,622**
- exact direct-source matches: **75,644,000 (99.8342%)**
- direct-source differences: **125,622**
- maximum absolute direct-source difference: **2.5**
- LAB Measurement, LAB Observation, OBS_CLIN Measurement, and OBS_CLIN Observation were all **100% exact**.
- all direct-source differences occurred in VITAL Measurement.

UCUM units:

- rows with unit semantics: **82,054,878**
- uniquely resolved active Standard UCUM rows: **58,916,347 (71.8012%)**
- unresolved under frozen case-sensitive UCUM policy: **23,138,531**
- exact agreement among resolved units: **58,916,347 / 58,916,347 (100%)**
- resolved disagreements: **0**

Categorical values:

- categorical VITAL rows: **2,170,885**
- prespecified mapped Standard values: **809,630 (37.2949%)**
- concept-zero policy rows: **1,361,255**
- exact mapped agreement: **809,630 / 809,630 (100%)**
- mapped disagreements: **0**
- unexpected nonzero targets where policy requires value concept 0: **0**

## VITAL numeric expression diagnostic

The read-only diagnostic reproduced both the direct native-field expression and the exact frozen ETL `CROSS APPLY (VALUES ...)` expansion before comparison with stored `measurement.value_as_number`.

Results:

- VITAL numeric rows: **11,673,550**
- direct native-field versus target differences: **125,622**
- frozen ETL expanded-expression versus target differences: **0**
- direct native-field versus expanded-expression differences: **125,622**
- direct-source differences explained by the frozen ETL expression: **125,622**
- unexplained differences after reproducing the frozen ETL expression: **0**

By field, the explained direct-source differences were HT 39,025, WT 39,745, and ORIGINAL_BMI 46,852; SYSTOLIC and DIASTOLIC had none. The maximum direct-source absolute difference was 2.5. Most differences were floating-point scale effects at <=1e-12, with a small number of larger WT/BMI differences.

Interpretation: the frozen OMOP target exactly reproduces the numeric value yielded by the frozen ETL SQL expression. Therefore these 125,622 rows are a deterministic value-representation/coercion effect of the frozen `VALUES` expression, not unexplained target divergence. No post-hoc tolerance was introduced and no ETL code was changed after observing this result.

## Manuscript and invariant bundle

`stage_b_wave2_manuscript_tables.py` was added in commit `5efbd1106d914fcf2570d3c1113ae58b6e695925`. It consumes only the already-produced aggregate Wave 2 outputs and fails if the prespecified reconciliation invariants do not hold. It also emits aggregate manuscript CSV/Markdown/JSON and an explicit disclosure review asserting that no row-level patient identifiers, source-record identifiers, PHI, or free-text clinical values are exported by the manuscript bundle.

## Next Wave 2 step

Run the Wave 2 manuscript/invariant bundle locally. If all invariants and the disclosure review pass, Wave 2 can be documented as analytically complete and Issue #4 can be closed without modifying the frozen ETL.
