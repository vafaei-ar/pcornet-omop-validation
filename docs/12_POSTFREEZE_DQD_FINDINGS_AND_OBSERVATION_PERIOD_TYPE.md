# 12 - Post-freeze DQD findings and observation-period type vocabulary review

**Evidence status:** 2026-11-01. Completed restricted, read-only DQD 2.9.1 on frozen `OMOP_VALIDATED_A.dbo`. This is a post-freeze quality assessment, not a change to the frozen ETL, the clinical target, Stage A-E results, or the study estimands. The separately approved one-row `CDM_SOURCE` metadata amendment preceded the corrected DQD execution.

**Controlling evidence:** operator-supplied local DQD console and aggregate checks, the frozen ETL implementation at commit `887e6f4d60a6b185e58b3c9fe8887472b49777e3`, the restricted local vocabulary read, and pinned OHDSI DQD v2.9.1 SQL. The local JSON, execution manifest, SQL query text, record numerators and denominators stay private pending disclosure review. This file includes no patient-level data or protected count estimates.

## 1. Executed DQD status and denominators

- Code verification: 320 Python tests passed on the institutional host.
- Restricted SQL security gate passed. The clinical CDM remained SELECT-only and protected clinical count anchors passed before and after execution. The DQD results were written only under `results/postfreeze_standard_qc/dqd_cohort_schema_fix_v1/`, preserving the first DQD attempt.
- Corrected cohort schema: `OMOP_VALIDATED_A.dbo`. DQD finished with launcher status 0 and **zero SQL error reports**. The initial run had 46 SQL errors from incorrectly resolving cohort tables in the QC-results schema. That configuration defect was fixed separately, without altering CDM data.
- **2,384 checks:** 1,440 passed, 47 failed, 897 not applicable, zero errors, zero unclassified. Among the 1,487 applicable checks, 96.8% passed. This is a check-count fraction, **not** PCORnet-to-OMOP record concordance.
- Failed checks by level: TABLE 4, FIELD 36, CONCEPT 7.
- Failed checks by DQD category: Completeness 29; Conformance/Computational 1; Conformance/Value 2; Plausibility/Atemporal 7; Plausibility/Temporal 7; Plausibility/unspecified subcategory 1. The JSON has no field named severity; DQD category is not a severity assessment.
- DQD v2.9.1 stores `pctViolatedRows` as `numViolatedRows / numDenominatorRows`, a **fraction**. It compares `pctViolatedRows * 100` with the configured threshold. Initial informal bins without the factor 100 were wrong. The operator verified consistency of fraction and count arithmetic for all 43 reportable failed checks.
- Corrected failure magnitude bands: 2 below 0.1%, 1 from 0.1% to below 1%, 14 from 1% to below 10%, 8 from 10% to below 100%, 18 exactly 100%, and 4 with suppressed small/unknown denominators. A 100% failure is **specific to the check and its eligible denominator**, not proof that every clinical record or the entire domain is inaccurate. Do not add percentages across checks or interpret them as unique affected people.

## 2. Exact fields for the 18 complete (100%) failures

| Finding group | Failed check fields | Number |
| --- | --- | ---: |
| Derived-table coverage | CONDITION_ERA: measureConditionEraCompleteness, measurePersonCompleteness; DRUG_ERA: measurePersonCompleteness | 3 |
| Record provenance/type concepts | DEATH.DEATH_TYPE_CONCEPT_ID; DEVICE_EXPOSURE.DEVICE_TYPE_CONCEPT_ID; MEASUREMENT.MEASUREMENT_TYPE_CONCEPT_ID; OBSERVATION.OBSERVATION_TYPE_CONCEPT_ID; PROCEDURE_OCCURRENCE.PROCEDURE_TYPE_CONCEPT_ID; SPECIMEN.SPECIMEN_TYPE_CONCEPT_ID; VISIT_OCCURRENCE.VISIT_TYPE_CONCEPT_ID | 7 |
| Units | DEVICE_EXPOSURE.UNIT_CONCEPT_ID; OBSERVATION.UNIT_CONCEPT_ID; SPECIMEN.UNIT_CONCEPT_ID | 3 |
| Visit admission/discharge provenance | VISIT_OCCURRENCE.ADMITTED_FROM_CONCEPT_ID; VISIT_OCCURRENCE.DISCHARGED_TO_CONCEPT_ID | 2 |
| Death cause and source cause | DEATH.CAUSE_CONCEPT_ID; DEATH.CAUSE_SOURCE_CONCEPT_ID | 2 |
| Observation-period type validity | OBSERVATION_PERIOD.PERIOD_TYPE_CONCEPT_ID (isStandardValidConcept) | 1 |
| **Total** | | **18** |

The existing restricted Achilles source-table inventory already established that `condition_era` and `drug_era` are empty, as are six other domains. The original freeze acceptance covered 11 populated target tables and did not certify complete OMOP domain coverage. Do not label the resulting era gaps a new DQD SQL execution defect.

Direct inspection of frozen ETL code explains some, but not necessarily all, of the other failures:

- `src/pcornet_omop_validation/etl/death.py` explicitly assigns `death_type_concept_id=0`, `cause_concept_id=0`, `cause_source_concept_id=0` and a NULL source cause in the frozen materialization, based on the recorded absence of source `PCORnet_DEATH_CAUSE` rows and the lack of defensible death-type mapping. These fields do **not** establish invalid death dates.
- `src/pcornet_omop_validation/etl/measurement.py` assigns `measurement_type_concept_id=0` on several paths. `procedure_occurrence.py` documents `procedure_type_concept_id=0` without an established PX_SOURCE-to-OMOP type mapping. `procedure_remaining_domains.py` materializes device/specimen type and unit concepts as zero for those routes. The underlying clinical event concept fields are distinct from these type concepts.
- Such assignments are documented mapping or source limitations. They remain legitimate limitations of downstream OMOP standardization and interoperability. Do not infer that every source field was missing or that all concept-zero values were unavoidable without a source-to-target audit.

## 3. Observation-period type: confirmed DQD failure mechanism

**Frozen ETL mapping:** `src/pcornet_omop_validation/etl/observation_period.py` derives `OBSERVATION_PERIOD` from `PCORnet_ENROLLMENT`. Its documented `ENR_BASIS` mapping is I/D -> 44814722, G -> 44814723, A -> 44814725, E -> 44814724; other values -> 0 under the explicit unmapped policy. These mappings and their provenance require independent evaluation before proposing replacements.

**Actual restricted vocabulary lookup on 2026-11-01** in `OMOP_VALIDATED_A.dbo.CONCEPT`, returning only the four public vocabulary definitions:

| Concept ID | Concept name | Domain | Vocabulary | `standard_concept` | `invalid_reason` |
| --- | --- | --- | --- | --- | --- |
| 44814722 | Period while enrolled in insurance | Type Concept | Obs Period Type | NULL | NULL |
| 44814723 | Period while enrolled in study | Type Concept | Obs Period Type | NULL | NULL |
| 44814724 | Period covering healthcare encounters | Type Concept | Obs Period Type | NULL | NULL |
| 44814725 | Period inferred by algorithm | Type Concept | Obs Period Type | NULL | NULL |

All four concept records exist and are not flagged invalid by `invalid_reason`. **None has `standard_concept='S'`.** These are separate properties. DQD v2.9.1 `inst/sql/sql_server/field_is_standard_valid_concept.sql` tests nonzero, joined concept IDs for `standard_concept='S'` and `invalid_reason IS NULL`. Consequently, records assigned any of the four IDs violate this DQD test regardless of the concepts' descriptive labels. The local result reports a 100% violation proportion for `OBSERVATION_PERIOD.PERIOD_TYPE_CONCEPT_ID`.

**Interpretation:** The immediate failure mechanism is **nonstandard legacy Type Concepts used as observation-period provenance values**. It is a vocabulary/ETL conformance limitation. It does **not** by itself prove invalid observation-period dates, incorrect patient counts, or a defective `ENR_BASIS` source field. The check does not independently assess whether a specific source value has the right provenance meaning. It is also not evidence that `standard_concept` in the vocabulary table should be edited.

**External context:** OHDSI's CDM 5.4 field specification says this value should represent **how the period was determined**. In an April 2021 OHDSI discussion on the same legacy ID 44814722, maintainers described refactoring Type Concepts and explained that provenance should describe the record's source rather than simply the covered period's meaning. This is supporting historical context, not proof that any particular replacement ID is valid in this exact local vocabulary release.

References:
- [CDM v5.4 OBSERVATION_PERIOD / PERIOD_TYPE_CONCEPT_ID](https://ohdsi.github.io/CommonDataModel/cdm54.html)
- [OHDSI discussion of type concept refactoring and ID 44814722](https://forums.ohdsi.org/t/questions-about-book-exercises-ch-4-and-5/13676)
- [DQD v2.9.1 SQL: field_is_standard_valid_concept](https://github.com/OHDSI/DataQualityDashboard/blob/v2.9.1/inst/sql/sql_server/field_is_standard_valid_concept.sql)
- [DQD v2.9.1 threshold evaluation](https://github.com/OHDSI/DataQualityDashboard/blob/v2.9.1/R/evaluateThresholds.R)
- [DQD NEWS, version 2.8.0 standard-valid-concept fix](https://github.com/OHDSI/DataQualityDashboard/blob/main/NEWS.md)

## 4. Disposition and next-version work, not a retroactive correction

**Frozen baseline:** Keep the original ETL, all clinical tables, the original study estimands and the corrected DQD JSON unmodified. The authorized metadata-only `CDM_SOURCE` insertion is independently documented in document 11. Do not edit `CONCEPT.standard_concept`, replace all old IDs with a single guessed ID, downgrade DQD, or adjust thresholds to create a nominal pass.

**Classification for study reporting:** (1) confirmed 100% check-specific standard-validity failure of observation-period type concepts; (2) documented incomplete derived-domain coverage; (3) known explicit source/semantic concept-zero assignments; (4) remaining unresolved DQD failures requiring field-specific investigation. Record the DQD findings as an additional post-freeze QC assessment, not a replacement for original source-to-target reconciliation.

**Potential future ETL version, after a separate review and approval:**

1. Inspect the frozen vocabulary's **current active Standard Type Concepts** and verify the appropriate `domain_id`, `concept_class_id`, status, and semantic definition. Do this against the actual local vocabulary snapshot, not inferred IDs or examples from an old book.
2. Map the provenance of each relevant `ENR_BASIS` category to how its observation-period **dates were established**, distinguishing enrollment evidence, encounter-based inference and algorithmic inference. Do not assume insurance *coverage type* necessarily implies the record's derivation method.
3. Design a source-justified mapping table; retain `ENR_BASIS`, lineage and unmapped reasons. Do not assign a Standard Type Concept when the source evidence is inadequate.
4. Use a **new versioned ETL build or isolated database**, never the frozen target. Validate domain, class, standard/invalid flags, dates, source coverage, lineage, cohort eligibility, and comparison with the original snapshot. Rerun the affected DQD checks only in that distinct build.
5. If a manuscript or regulatory claim depends on this issue, quantify its effect on the **relevant downstream cohort/exposure interpretation** and apply a disclosure review. Do not treat a high DQD pass fraction as proof of scientific concordance.

**Suggested manuscript-safe statement:** “Post-freeze OHDSI Data Quality Dashboard 2.9.1 assessment completed without SQL execution errors. Of 2,384 checks, 1,440 passed, 47 failed, and 897 were not applicable. Failures included known unpopulated derived domains and deliberately unmapped supporting concepts. A targeted review found that the observation-period type mapping referenced existing but nonstandard legacy Type Concepts, resulting in a 100% failure for the corresponding DQD validity check. These quality findings do not directly measure PCORnet-to-OMOP study reproducibility.” Subject to disclosure and coauthor review.

**Unresolved:** Neither this review nor the four-definition vocabulary query identifies an approved replacement Standard Type Concept, establishes the distribution of each `ENR_BASIS` category in the clinical data, or explains all 47 failed DQD checks. Do not claim otherwise.
