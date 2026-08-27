# Stage B Wave 1 progress

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked study definition: `study_definitions/stage_b_v1.json`

This document records Stage B Wave 1 implementation and results as they are generated. Primary concordance uses independently defined native PCORnet and OMOP queries. Frozen ETL lineage/xwalk tables are used only after the primary comparison for discordance attribution.

## Preflight

Stage B Wave 1 preflight completed successfully on 2026-08-27.

- study definition: `stage-b-v1`
- patient linkage: `matched_unique_source_bridge`
- source tables present: 5
- target tables present: 10
- lineage tables present: 14
- analysis worktree: clean

## Encounter / Visit concordance

The first native-CDM patient-level comparison completed successfully.

Primary results:

- PCORnet eligible encounters: **1,510,957**
- OMOP visit rows: **1,510,957**
- source patients with encounters: **27,087**
- OMOP patients with visits: **27,087**
- shared patients: **27,087**
- source-only patients: **0**
- OMOP-only patients: **0**
- patient Jaccard: **1.0**
- patients with unequal event counts: **0**
- exact person + start-date matched events: **1,510,957**
- source unmatched date events: **0**
- OMOP unmatched date events: **0**

Interpretation: under the pre-specified Stage B encounter definition, patient presence, per-patient encounter counts, and exact calendar start-date event multisets are fully concordant between native PCORnet ENCOUNTER and frozen OMOP Visit Occurrence. This result was computed without using the encounter-to-visit xwalk in the primary metrics.

Encounter type is descriptive rather than an exact-equality acceptance criterion because PCORnet encounter categories and OMOP Standard Visit concepts are representationally different semantic systems. Source encounter type and OMOP visit concept/source value distributions are retained as secondary characterization outputs.

## Death concordance

Implementation is committed and is the next Wave 1 run. The primary comparison is locked to:

- patient-level death presence;
- exact calendar death date;
- native PCORnet DEATH versus native OMOP death queries using the fixed patient bridge;
- no death xwalk in primary metrics;
- death lineage only for secondary attribution.

Death type and cause concept equality are not Stage B concordance requirements because the frozen ETL explicitly retains concept `0` where source provenance/cause semantics do not justify an exact OMOP concept.

## Remaining Wave 1 sequence

```mermaid
flowchart LR
    A[Preflight complete] --> B[Encounter complete]
    B --> C[Death]
    C --> D[Condition semantics]
    D --> E[Procedure semantics]
    E --> F[Wave 1 aggregate manuscript tables]
    F --> G[Disclosure review]
```

Condition and Procedure comparisons will preserve the locked rule that one-to-many and cross-domain Standard mappings are not automatically discordant. Primary metrics will be based on independently constructed semantic target representations; lineage will be used only afterward to classify disagreement.
