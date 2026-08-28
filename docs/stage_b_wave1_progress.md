# Stage B Wave 1 progress

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked study definition: `study_definitions/stage_b_v1.json`

This document records Stage B Wave 1 implementation and results as they are generated. Primary concordance uses independently defined native PCORnet and OMOP queries where possible. For semantic domains that require vocabulary normalization, the frozen route ledger is used only as the prespecified source-side semantic reference; target OMOP event retrieval remains native and target lineage/xwalks are reserved for secondary discordance attribution.

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

- PCORnet eligible encounters: **1,510,957**
- OMOP visit rows: **1,510,957**
- source and OMOP patients: **27,087** each
- patient Jaccard: **1.0**
- patients with unequal event counts: **0**
- exact person + start-date matched events: **1,510,957**
- unmatched date events: **0** on both sides

Interpretation: under the pre-specified Stage B encounter definition, patient presence, per-patient encounter counts, and exact calendar start-date event multisets are fully concordant. The visit xwalk was not used in the primary metrics.

## Death concordance

The native-CDM death comparison completed successfully.

- PCORnet eligible death events and OMOP death rows: **6,955** each
- source and OMOP patients: **6,955** each
- patient Jaccard: **1.0**
- exact death-date matches: **6,955**
- discordant date pairs: **0**

Interpretation: patient-level death presence and exact calendar death date are fully concordant. Death type and cause concept equality are not Stage B concordance requirements because the frozen ETL explicitly retains concept `0` where source provenance/cause semantics do not justify an exact OMOP concept.

## Condition semantic concordance

The optimized Condition comparison completed successfully.

- eligible DIAGNOSIS + CONDITION events: **8,674,973**
- core semantic routes: **9,043,769**
- mapped nonzero Standard routes: **8,983,621**
- unresolved concept-0 fallback: **60,148**
- multi-route source events: **361,606**
- source mapped patients: **25,614**
- native OMOP patients in the same concept space: **26,388**
- patient Jaccard before attribution: **0.9706684857**
- exact person/date/domain/concept matched events: **8,983,621**
- source unmatched mapped events: **0**
- target unmatched rows in the same concept space: **756,113**

Secondary lineage attribution showed:

- native OMOP rows in Condition semantic concept space: **9,739,734**
- Condition-derived rows: **8,983,621**
- other audited provenance: **756,113**

Thus every mapped Condition semantic route was present exactly, and the entire target-side excess was explained by other source families populating the same OMOP Standard concept/domain space. Concept `0` fallback remains a separate unresolved-coverage result.

## Procedure semantic concordance

The optimized Procedure comparison also completed successfully.

- eligible source events: **11,228,023**
- all route rows: **11,234,863**
- mapped nonzero event routes: **11,121,561**
- unresolved routes: **111,660**
- non-event semantic components: **1,642**
- source events with multiple mapped event routes: **6,769**
- source mapped patients: **26,951**
- native OMOP patients in the same mapped concept space: **27,074**
- source-only patients: **0**
- target-only patients: **123**
- patient Jaccard before attribution: **0.9954568959**
- exact person/date/domain/concept matched mapped events: **11,121,561**
- source unmatched mapped events: **0**
- target unmatched rows in the same concept space: **1,537,643**

Every mapped Procedure semantic event was therefore found exactly in native OMOP. The target-side excess is concentrated in shared semantic spaces, especially Condition (**469,596** extra rows), Drug (**516,168**), Observation (**381,904**), Measurement (**136,881**), Procedure (**32,773**), Device (**292**), and Specimen (**29**). These rows are not treated as ETL discordance until secondary provenance attribution is completed, because OMOP domains can legitimately receive events from several PCORnet source families.

A secondary Procedure attribution module is now implemented and will classify those target-side rows using frozen lineage only after this primary result. No primary matching rule is changed by that attribution.

## Current Wave 1 interpretation

The completed primary comparisons show a consistent pattern:

1. Encounter and Death have exact native-CDM concordance.
2. Condition and Procedure have **100% exact mapped-source semantic event recall** under person + date + OMOP domain + Standard concept multiset matching.
3. Native OMOP target queries can return additional rows in the same semantic concept space because multiple PCORnet source families converge on common OMOP domains and concepts.
4. Secondary lineage attribution is therefore needed to distinguish legitimate multi-source semantic overlap from unexplained transformation discordance.

```mermaid
flowchart LR
    A[Preflight complete] --> B[Encounter exact]
    B --> C[Death exact]
    C --> D[Condition primary exact]
    D --> E[Condition excess fully attributed]
    E --> F[Procedure primary exact]
    F --> G[Procedure attribution]
    G --> H[Wave 1 manuscript tables]
    H --> I[Disclosure review]
```

The frozen ETL remains unchanged. Counts in this document are analysis outcomes, not acceptance thresholds.
