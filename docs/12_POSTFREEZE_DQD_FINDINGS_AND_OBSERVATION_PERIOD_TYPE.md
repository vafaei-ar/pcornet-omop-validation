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


## 5. Complete 47-check triage against the frozen study

**Review date:** 2026-11-01. This classification reconciles the 47 failed DQD check *names and fields* furnished by the institutional operator to the frozen ETL code and the locked Stage C-E definitions. It is not a patient-level root-cause audit. The local DQD result file was **not accessible remotely** for examination of each record, `checkId`, `thresholdValue`, `queryText`, or cohort overlap. Therefore, categories labeled *unresolved* must remain unresolved. The 18 individually confirmed full-violation checks above are a subset of this full inventory.

### 5.1 All 47 failure instances, without restricted counts

| DQD test / level | Exact affected field(s) | Number | Evidence-based classification | Study relevance |
| --- | --- | ---: | --- | --- |
| `standardConceptRecordCompleteness`, FIELD | CONDITION_OCCURRENCE.CONDITION_STATUS_CONCEPT_ID, CONDITION_TYPE_CONCEPT_ID | 2 | Type/status mapping coverage, with deliberate fallback to concept 0 for non-Standard or unapproved source categories; source mix/violating rows not examined | Supporting phenotype semantics; the stroke phenotype uses condition clinical concept and dates, not these two supporting concept fields |
| Same | DEATH.CAUSE_CONCEPT_ID, DEATH_TYPE_CONCEPT_ID | 2 | **Explained by frozen policy**: missing cause source rows and no justified death-type inference lead to zero codes | Death provenance, not the 30/90-day acute-care endpoint |
| Same | DEVICE_EXPOSURE.DEVICE_TYPE_CONCEPT_ID, UNIT_CONCEPT_ID | 2 | **Explained for frozen procedure routes**: ETL assigns zero to these supporting fields | Relevant to device studies, not locked stroke endpoint |
| Same | DRUG_EXPOSURE.DRUG_CONCEPT_ID, DRUG_TYPE_CONCEPT_ID | 2 | Primary drug mapping incompleteness and type-concept coverage; the ETL records substantial intentional zero Drug routes, but source-specific affected rows and type-validity defect remain **unresolved** | Drug phenotyping and external OHDSI analyses; not direct Stage C-E model features |
| Same | MEASUREMENT.MEASUREMENT_TYPE_CONCEPT_ID, UNIT_CONCEPT_ID | 2 | Type zero is explicit on several ETL paths; unit coverage may reflect unmapped source UCUM or source missingness, not assumed solved by type policy | Relevant to measurement research; Stage E locked model does not use these variables |
| Same | OBSERVATION.OBSERVATION_CONCEPT_ID, OBSERVATION_TYPE_CONCEPT_ID, UNIT_CONCEPT_ID | 3 | Primary observation concept coverage **unresolved**; supporting type and unit semantics are separate problems | External observation-based phenotypes; locked D0 acute-care endpoint not defined using these fields |
| Same | PROCEDURE_OCCURRENCE.PROCEDURE_TYPE_CONCEPT_ID | 1 | **Explained by frozen policy**: type zero when PX_SOURCE lacks an approved OMOP mapping | Procedure provenance and transportability |
| Same | SPECIMEN.SPECIMEN_TYPE_CONCEPT_ID, UNIT_CONCEPT_ID | 2 | **Explained for frozen procedure-derived routes**: supporting fields set to zero | Specimen-based downstream research |
| Same | VISIT_OCCURRENCE.ADMITTED_FROM_CONCEPT_ID, DISCHARGED_TO_CONCEPT_ID, VISIT_CONCEPT_ID, VISIT_TYPE_CONCEPT_ID | 4 | Type and admission/discharge zero are **explicit frozen policy**; primary `VISIT_CONCEPT_ID` mapping completeness must be assessed separately | **Potential direct impact**: D0 index and acute-care endpoint query `VISIT_CONCEPT_ID` and dates. Locked concordance provides empirical reassurance in tested subsets, not global proof |
| `sourceConceptRecordCompleteness`, FIELD | DEATH.CAUSE_SOURCE_CONCEPT_ID | 1 | **Explained by frozen cause-policy** and source `DEATH_CAUSE` absence | Provenance, not direct endpoint |
| Same | DRUG_EXPOSURE.DRUG_SOURCE_CONCEPT_ID; MEASUREMENT.MEASUREMENT_SOURCE_CONCEPT_ID; OBSERVATION.OBSERVATION_SOURCE_CONCEPT_ID | 3 | **Unresolved source vocabulary coverage**, distinguish source concept ID from source code/value retention | Source traceability and untested generalizability |
| `sourceValueCompleteness`, FIELD | VISIT_OCCURRENCE.VISIT_SOURCE_VALUE | 1 | **Unresolved**: ETL attempts to retain `ENC_TYPE`, but some eligible source values may be blank or otherwise filtered | Source encounter classification provenance, possible external reclassification |
| `isStandardValidConcept`, FIELD | DRUG_EXPOSURE.DRUG_TYPE_CONCEPT_ID | 1 | **Unresolved nonstandard/invalid nonzero Drug Type concept**. Frozen `_validate_type_concepts` verifies ID/domain/vocabulary/code and `invalid_reason` but does **not** verify `standard_concept='S'`; DQD does. Must inspect local vocabulary and affected ETL family before attribution | OHDSI drug type validity, not directly used by locked stroke study |
| Same | OBSERVATION_PERIOD.PERIOD_TYPE_CONCEPT_ID | 1 | **Confirmed:** legacy nonstandard Type Concepts (four validated IDs with Standard flag NULL), per section 3 | OMOP conformance. Locked Stage D uses observation-period **dates**, not the type field |
| `fkClass`, FIELD | DRUG_STRENGTH.INGREDIENT_CONCEPT_ID | 1 | **Unresolved vocabulary integrity**: DQD expects ingredient concept class for nonzero references. `DRUG_STRENGTH` is a vocabulary table, not an ETL-generated clinical event table | Vocabulary reuse/medication analytics; not direct Stage C-E endpoint |
| `measurePersonCompleteness`, TABLE | CONDITION_ERA, DRUG_ERA | 2 | **Explained by known empty derived tables**, frozen acceptance scope | External era-based phenotypes are unsupported; locked Stage C-E does not query era tables |
| Same | OBSERVATION_PERIOD | 1 | **Unresolved coverage**. Important: dates/coverage are used for follow-up eligibility; compare person coverage and eligible-cohort overlap before concluding irrelevance | **Potential direct impact** to 30/90/365-day observability, despite fixed-cohort outcome agreement |
| `measureConditionEraCompleteness`, TABLE | CONDITION_ERA | 1 | **Explained by known empty derived table** | Same era limitation as above |
| `plausibleAfterBirth`, FIELD | MEASUREMENT.MEASUREMENT_DATE and MEASUREMENT_DATETIME | 2 | **Unresolved date plausibility**. DQD compares event date to DOB or DOB inferred from available year/month/day | Clinical chronology. Measurement is not part of the locked primary stroke endpoint, but may affect other studies |
| `plausibleBeforeDeath`, FIELD | CONDITION_OCCURRENCE.CONDITION_END_DATE, CONDITION_END_DATETIME; MEASUREMENT.MEASUREMENT_DATE, MEASUREMENT_DATETIME | 4 | **Unresolved**: pinned DQD checks more than **60 days after recorded death**, not merely later than death. Source-reported death date, condition end-date semantics and late-reporting artifacts require local review | Could indicate chronology problems, but locked D0 phenotype uses condition start, not end date |
| `plausibleTemporalAfter`, FIELD | MEASUREMENT.MEASUREMENT_DATE | 1 | Deprecated predecessor temporal check; **unresolved and potentially overlapping** with `plausibleAfterBirth`. Must not double-count affected records | Secondary/measurement analyses |
| `withinVisitDates`, FIELD | MEASUREMENT.MEASUREMENT_DATE | 1 | **Unresolved**: pinned DQD defines an outlier only beyond **seven days before visit start or after visit end**. Assess source event-vs-visit timestamp and the visit link | Linked measurement chronology, not primary acute-care visit event |
| `plausibleGender`, CONCEPT | Seven concept-level failures (exact `conceptId` values not supplied) | 7 | Deprecated legacy rule; 287 old checks had 7 failures, while 4 descendants-based replacement checks had 0. These are **different check sets**, so zero replacement failures cannot erase legacy evidence. No patient-level inference without concept IDs and denominators | Potential phenotype generalizability; do not equate clinical plausibility with an error in gender identity or sex at birth |
| **TOTAL** | | **47** | 29 Completeness, 3 Conformance, 15 Plausibility | |

The table is grouped by check type and field, not by unique affected patient. Several check names test different fields of the *same* records. DQD reports failure on the eligible denominator of each check. No unique-patient or downstream-overlap estimate can be obtained by summing these checks.

### 5.2 Do these findings change the original Stage C-E conclusions?

**Read-only code-based dependency review:** `src/pcornet_omop_validation/study/stage_c_stroke_d0_concordance.py` selects stroke events using `condition_concept_id`, visit concepts and dates. The locked `stage_d_stroke_analytical_equivalence.py` selects acute-care events from `visit_occurrence.visit_concept_id` and visit start, and follow-up eligibility from `observation_period_start_date/end_date`. It does **not** filter on `period_type_concept_id`, visit type/disposition, measurement type, unit, or drug type. The Stage E design includes age, female indicator, LOS, prior visit counts and prior stroke, not drug/measurement/specimen/device concepts.

| Scientific question | Relevant DQD findings | Decision supported by existing evidence | Residual risk |
| --- | --- | --- | --- |
| **Stroke index phenotype (Stage C)** | Primary `VISIT_CONCEPT_ID` completeness; supporting condition type/status and gender-related concept plausibility | The existing mapped-condition fidelity and harmonized nonmissing-DX_DATE phenotypes remain documented. No DQD evidence overturns the identified diagnosis-date policy mechanism | Whether failed visit concepts/legacy gender concepts intersect source-only or shared stroke episodes is not shown |
| **90-day/30-day acute-care outcome (Stage D)** | Primary `VISIT_CONCEPT_ID` completeness and `OBSERVATION_PERIOD` person coverage | Existing **fixed-index** matched results show exactly equal eligible outcomes, event counts and first-event dates. Observation-period **type validity** is not a field used in the locked outcome code | Empty/missing coverage for selected persons and possible alternate-phenotype effects cannot be ruled out without restricted overlap audit. Do not claim all observation-period semantics valid |
| **Modeling (Stage E)** | `VISIT_CONCEPT_ID`, observation-period dates, possible gender-related plausibility | Existing fixed-cohort features and model comparisons remain the controlling empirical evidence. DQD supporting-type/unit concept flags do not directly enter the six locked predictors | End-to-end cohort selection still matters, and gender concept plausibility deserves separate clinical/contextual review if that construct is interpreted clinically |
| **General OMOP reuse / OHDSI interoperability** | Era tables, primary drug/observation concept gaps, type/units/visit provenance, `DRUG_STRENGTH` class | **Material limitations**: the database is not a fully populated or fully DQD-conformant OMOP instance | Other externally defined drug, era, measurement, visit provenance or observation phenotypes could produce different conclusions |

**Scientific verdict:** The 47 DQD failures **do not refute** the source-to-target fidelity and fixed-cohort reproduction results already measured in the locked study. They **do restrict generalizability**, especially to eras, standard concept-based drug/observation studies, and provenance-sensitive analyses. The confirmed observation-period type issue is **not a demonstrated causal mechanism** for the published end-to-end stroke-cohort divergence, because the study did not query that type field. Its immediate DQD mechanism is fully explained. The effect of incomplete observation-period person coverage and failed primary visit concepts on the studied cohorts is **not proven absent**, although direct fixed-cohort concordance constrains possible effects.

The most important analytical risk is conflating **ETL adherence to an explicit, sometimes incomplete policy** with **complete OMOP research readiness**. The study can support conditional fidelity claims and a policy-driven end-to-end selection mechanism, but it cannot claim all OMOP domains are fit for arbitrary studies.

### 5.3 Prioritized, bounded next steps and stopping rules

1. **Highest relevance to original claims, still unresolved:** If a manuscript claims complete observability or general phenotype portability, perform a *restricted local* overlap audit between the DQD-failed `VISIT_CONCEPT_ID` cases, the observation-period missing-coverage population, and the locked Stage C/D cohort IDs. Publish only disclosure-reviewed aggregate categories. Without that audit, use a narrowed claim restricted to the already tested outcomes and cohorts.
2. **Vocabulary conformance to repair only in a future ETL:** Check local `standard_concept` and validity for the configured `DRUG_TYPE_CONCEPT_ID` family IDs. A code review found the frozen Drug Type validator does not require `standard_concept='S'`, unlike the frozen Condition mapping validator, so it can accept a nonstandard concept. This is a prospective validator test, not justification for an automatic conversion or frozen-database edit.
3. **Remaining local data-interpretation questions:** Trace the source and linked visit for failed measurement dates and condition end dates; inspect source source-code vocabulary gaps; interpret the seven concept-level gender findings using actual concept IDs and context. Do not publish small cell sizes or unreviewed exact percentages.
4. **Study publication closeout:** If the purpose is only to report the *already locked* A-E findings with appropriately qualified domain limitations, no extra SQL run is essential. Record DQD as a completed, independent, supplementary post-freeze QC analysis. Maintain the unmodified frozen snapshot and do not tune the DQD failure thresholds to improve a score.

### 5.4 Audit boundaries

This classification is based on an operator-provided list of **47 field/check failures**, summary magnitude bands and four public vocabulary definitions, as well as inspectable frozen ETL and study source. It does **not** verify each failure's individual threshold, `checkId`, affected record distribution, source cause or cohort intersection. Unknown magnitudes among the remaining 29 non-100%-failure instances may still be important. It is invalid to call the remaining 29 'minor' merely because their violation percentage is below 100%.

The corrected DQD report is saved locally under `results/postfreeze_standard_qc/dqd_cohort_schema_fix_v1` and must remain outside version control until an authorized disclosure review. No private SQL result, patient ID, or raw check query is included here.
