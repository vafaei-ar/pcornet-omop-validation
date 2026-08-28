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

This supports the same interpretation established in Wave 1: native OMOP concept-space excess must be attributed before being labeled discordant. In this case the excess is extremely small and fully explained by other audited source provenance.

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

## Next Wave 2 step

The next step is Measurement/Observation preflight and denominator decomposition before event-level outcome comparison. It must confirm source-family lineage structure for LAB, VITAL, OBS_CLIN, OBS_GEN, Procedure-derived Measurement/Observation, and Condition-derived cross-domain Measurement/Observation, and lock which subsets enter semantic-presence, numeric-value, unit, and categorical-value layers.
