# Stage C stroke D0 phenotype reproducibility specification

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked definition: `study_definitions/stage_c_stroke_d0_v1.json`

Status: **prespecified before Stage C D0 outcome queries**

## Objective

Stage C asks a different question from Stage B. Stage B established that mapped source semantics are present in the frozen OMOP build under prespecified event-level rules. Stage C now asks whether a complete computable phenotype selects the same patients after transformation.

The first phenotype is the PSU PROMIS EHR-only ischemic-stroke D0 definition already represented in `stroke_codes.py` and `pcornet_stroke_phenotypes.py`. This specification freezes the D0 rules before running the publication D0 comparison.

## Source-reference D0

The primary PCORnet reference phenotype is:

- exact explicit ischemic-stroke ICD code membership after uppercasing, trimming, and removing decimal points;
- `PDX == 'P'`;
- encounter type `EI` or `IP`;
- at least one overnight stay, defined as a calendar-day difference of at least one day between admission and discharge dates;
- for each encounter, choose the first qualifying primary stroke diagnosis ordered by `DX_DATE` with nulls last and then normalized DX;
- define the encounter index date as `COALESCE(DX_DATE, ADMIT_DATE, DISCHARGE_DATE)`;
- choose the first qualifying encounter per patient ordered by index date and encounter identifier;
- only after index selection, require `floor(days(BIRTH_DATE, INDEX_DATE) / 365.0) >= 18`;
- no registry gating or registry augmentation.

A recognized-`DX_TYPE` restriction is not part of the primary D0 phenotype. It may be reported only as a diagnostic sensitivity.

## Why Stage C has two OMOP estimands

A critical representation issue is fixed before outcome comparison: the source phenotype requires `PDX`, but the frozen OMOP core representation does not encode the PCORnet `PDX` field as a native OMOP phenotype attribute. Treating this as though it had a native one-to-one OMOP equivalent would silently change the phenotype.

Therefore Stage C D0 has two distinct estimands.

### Primary: transformation-fidelity D0

The primary analysis tests whether the **exact source-defined phenotype semantics** survive the transformation. Native OMOP person, visit, condition, and date fields are used for transformed events, while frozen lineage may be used only for source-only semantics that cannot be expressed natively, especially `PDX == 'P'` and exact source diagnosis-code identity.

This is the appropriate estimand for asking whether the frozen transformation preserves the original D0 cohort.

### Secondary: native-OMOP portable D0

A separate sensitivity analysis expresses a D0-like phenotype using OMOP-native semantics only:

- the explicit locked ICD code list is resolved through the frozen vocabulary to active Standard Condition concepts using exact source concepts and `Maps to` relationships, preserving one-to-many mappings;
- EI and IP use the frozen Standard Visit mappings `262` and `9201`;
- overnight stay and adult age use OMOP dates/person data;
- `PDX == 'P'` is intentionally omitted because it is not natively representable in the frozen OMOP core.

This portable phenotype is not allowed to replace the primary transformation-fidelity comparison. Its difference from the source cohort is itself a model-representability result.

## Metrics

The primary comparison reports PCORnet and lineage-faithful OMOP cohort sizes, shared patients, source-only and OMOP-only patients, Jaccard similarity, positive agreement, and exact index-date agreement. Index dates within one day are secondary. The native-portable OMOP cohort is reported separately.

Discordance is decomposed into predefined categories such as required source-date exclusion, missing visit or condition materialization/lineage, source-only primary-diagnosis semantics, index-date representation, age boundary, multiple-event ordering, and unexplained differences.

No patient-level identifiers or disagreement rows are committed. Only aggregate, disclosure-reviewed summaries may enter Git.

## D1 and D3 are deliberately deferred

D1 and D3 require the exact lipid LOINC whitelist used by the PROMIS pipeline. The existing verification code discovers that list from an external CSV. For publication reproducibility, the whitelist must first be versioned or otherwise hashed as a study artifact before D1/D3 are locked and queried. Their imaging and lipid windows will likewise be fixed before outcome comparison.

```mermaid
flowchart TD
    A[Locked explicit stroke code list] --> B[PCORnet source-reference D0]
    C[Frozen audited OMOP] --> D[Lineage-faithful OMOP D0]
    C --> E[Native-OMOP portable D0 sensitivity]
    F[Frozen lineage] --> D
    B --> G[Primary cohort reproducibility comparison]
    D --> G
    E --> H[Representability sensitivity]
    G --> I[Prespecified discordance decomposition]
    H --> I
    I --> J[Aggregate disclosure-reviewed Stage C results]
```

## Freeze rule

No ETL mapping, stroke code, D0 rule, matching rule, or discordance category may be changed merely because a later comparison improves or worsens agreement. Any correction after outcome inspection requires an independently demonstrated methodological defect and an explicit decision-log entry.
