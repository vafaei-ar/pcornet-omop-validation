# Current status and publication plan

_Last updated: 2026-08-27_

## Current status

The audited PCORnet-to-OMOP ETL has completed its final clean, code-frozen rebuild and is now the publication ETL freeze for subsequent scientific analyses.

The frozen ETL commit is:

`887e6f4d60a6b185e58b3c9fe8887472b49777e3`

The final rebuild was executed from an empty validated target against `OMOP_VALIDATED_A`, using `config/etl_A.yaml`, with the comparator/prior OMOP database left untouched. Phase 14 recorded a clean worktree (`dirty_worktree_entries: 0`) and hashed the ETL source files and audit JSON files used for the build.

This freeze is accepted on reconciliation, required-field/interval integrity, vocabulary-semantic validity, explicit ambiguity/exclusion policy, and documented review decisions. Row counts are outputs of the current source and vocabulary semantics, not acceptance thresholds.

## Final freeze acceptance results

The final Phase 12-14 validation reported:

- global reconciliation: **matched**;
- visit-time semantics: **matched**;
- materialized visit datetime mismatches: **0**;
- duplicate primary-key groups: **0**;
- reversed intervals in audited interval-bearing tables: **0**;
- semantic hard blockers: **0**;
- semantic review flags: **10**;
- explained review flags: **10**;
- unexplained review flags: **0**;
- auxiliary concept blockers: **0**;
- final manifest worktree dirtiness: **0**.

The remaining semantic review flags are explicit concept-0/provenance decisions rather than hidden failures. They include type-concept zeros in several domains, unresolved standardized drug routes, and the explicit Death provenance policy.

## Final frozen OMOP target counts

| OMOP table | Rows |
| --- | ---: |
| person | 27,089 |
| observation_period | 27,087 |
| visit_occurrence | 1,510,957 |
| condition_occurrence | 7,315,572 |
| procedure_occurrence | 4,182,803 |
| measurement | 85,715,435 |
| observation | 7,319,081 |
| drug_exposure | 48,458,058 |
| device_exposure | 196,660 |
| specimen | 93 |
| death | 6,955 |

## What was completed to reach the freeze

### ETL architecture and auditability

The conversion was redesigned or hardened so that clinically meaningful transformation decisions are explicit and reproducible. The implementation now uses configurable source/target schemas, quantified required-date exclusions, vocabulary-driven routing, route ledgers, lineage/xwalk tables, explicit concept-0 fallback, and clean-reset safeguards that operate only on the configured validated target.

Exact source-vocabulary mappings do not choose arbitrary `TOP(1)` candidates. Ambiguous mappings fail closed unless a unique defensible Standard mapping exists. Nonzero target concepts are validated against active OMOP concepts and expected domains where field semantics justify an expected domain.

### Person

Gender, race, and ethnicity candidate mappings are validated against the loaded OMOP vocabulary. Deprecated/non-Standard candidates are rejected to concept `0` while source values are preserved. The prior race-concept freeze blocker was removed. The existing-target idempotence validation path was also fixed and directly re-tested on the populated target, returning `already_loaded_matched` for all 27,089 Person rows.

### Visit occurrence

Encounter-type mapping is conservative and source-derived. Numeric PCORnet visit times were empirically profiled and validated as SAS seconds within a day. The final audit found zero materialized datetime mismatches.

### Procedure routing

PCORnet PROCEDURES are represented through a route ledger supporting direct Standard mappings, `Maps to`, one-to-many mappings, unresolved fallback, cross-domain event routing, and non-event semantic components. Non-event semantic components are retained in the route ledger rather than materialized as false standalone clinical events.

### OBS_CLIN

OBS_CLIN is routed according to validated Standard concept domain into Measurement, Observation, or Condition. Ambiguous/unresolved mappings are not forced into Measurement.

### Condition

DIAGNOSIS and CONDITION use a canonical event-route model. Multi-target `Maps to` routes are supported; `Maps to value` and other non-event targets do not independently create events. Source events with only non-event targets receive an explicit Condition concept-0 fallback. Condition type/status candidates are validated against the exact expected OMOP semantic domains; non-Standard or otherwise invalid candidates become `0` rather than being forced.

### Measurement

LAB, VITAL, Procedure-derived Measurement, and OBS_CLIN Measurement rows reconcile through lineage. LOINC/UCUM mapping is source-derived, and exact UCUM matching is case-sensitive. Unsupported/ambiguous units remain concept `0` rather than being guessed.

### Observation

Observation materialization combines routed OBS_CLIN, OBS_GEN, LAB, Procedure, and VITAL components with source-specific lineage and reconciliation.

### Drug exposure

Drug route construction covers PRESCRIBING, DISPENSING, MED_ADMIN, IMMUNIZATION, and Procedure-derived drug events. RxNorm/NDC handling is vocabulary-driven and supports one-to-many mappings. Route finalization maps only uniquely defensible active Standard Route concepts; unresolved standardized route values remain concept `0` and are explicitly reviewed.

### Device, Specimen, and cross-domain events

Procedure-derived and Condition-derived cross-domain routes are materialized into their corresponding OMOP domains with lineage and duplicate-route/target checks.

### Death

Death materialization is conservative: required fields are enforced, no sentinel date is invented, and the present source has no DEATH_CAUSE rows. Unsupported death type/cause semantics remain `0` under a documented provenance policy.

## Final clean-build execution

The publication freeze build was run after a guarded reset that left PCORnet staging and the vocabulary intact while emptying the 11 derived OMOP core targets and removing ETL route/xwalk tables.

The build then completed all 14 phases:

1. Person, Observation Period, Visit Occurrence
2. Procedure, OBS_CLIN, and Condition route ledgers
3. Primary Condition and Procedure events
4. Base Measurement
5. OBS_CLIN Measurement append
6. Observation
7. OBS_CLIN Condition append
8. Drug routes, Drug Exposure, and Route finalization
9. Remaining Procedure-routed Condition/Device/Specimen events
10. Condition cross-domain events
11. Death
12. Visit-time, global reconciliation, and semantic validation
13. Explicit review decisions
14. Freeze manifest

All materialization phases reported matched reconciliation for the components they own.

## Frozen ETL versus ongoing publication work

The ETL freeze is immutable for the scientific comparison unless a new, independently justified ETL defect is discovered. Publication-analysis code may continue to evolve, but it should treat the frozen ETL commit and frozen database build as fixed inputs. Downstream analyses must not change ETL semantics merely to improve PCORnet-versus-OMOP concordance.

A dedicated publication-analysis branch, `publication/analysis`, starts from the frozen ETL commit so downstream study work can proceed without moving the ETL freeze point.

## Publication analysis plan

### Stage A — structural and semantic concordance

Quantify what is preserved, expanded, routed to another OMOP domain, unresolved, or excluded during ETL. Planned outputs include source-event eligibility/exclusions, route and target distributions, one-to-many expansion, concept-0 rates and reasons, visit linkage, unit/route coverage, and cross-domain routing summaries.

### Stage B — patient-level semantic concordance

Define comparable patient-level clinical variables independently in PCORnet and the frozen OMOP build. Compare diagnosis, procedure, medication, measurement, encounter, and temporal representations using patient overlap and agreement metrics. Distinguish representational differences from actual information loss.

### Stage C — phenotype reproducibility

Implement selected phenotypes independently in both CDMs, then compare cohort size, overlap, positive/negative agreement, discordance mechanisms, and sensitivity to representation choices. Existing stroke-study modules provide a starting point for this stage, but phenotype definitions must be locked before outcome comparison.

### Stage D — analytical equivalence

Run matched downstream analyses in both CDMs and compare estimates, uncertainty, and conclusions. Candidate analyses include descriptive rates, associations, time-to-event analyses, and pre-specified sensitivity analyses linked to documented ETL/CDM representation differences.

## Immediate next steps

1. Preserve the final Phase 14 manifest and associated audit bundle as the ETL reproducibility record.
2. Build a publication-analysis manifest that records the frozen ETL SHA, analysis-code SHA, source/target identifiers, study-definition version, and output hashes.
3. Produce Stage A structural/semantic concordance tables directly from the existing route ledgers, xwalks, and audit JSON files.
4. Lock the first phenotype specification before inspecting PCORnet-versus-OMOP outcome differences.
5. Run patient/cohort concordance and discordance decomposition.
6. Generate manuscript-ready tables/figures from scripted aggregate outputs only.
7. Draft Methods and Results with ETL-validation results separated from scientific PCORnet-versus-OMOP results.

## Reproducibility bundle for publication

The publication bundle should preserve, without committing protected data:

- frozen ETL Git SHA;
- analysis-code Git SHA;
- publication ETL configuration with secrets removed;
- source file inventory/hashes where permissible;
- vocabulary version/provenance;
- ETL source-file hashes;
- final Phase 1-14 audit JSON files;
- route/lineage reconciliation summaries;
- exclusion and concept-0 summaries;
- semantic review decisions;
- final freeze manifest;
- scripts producing manuscript tables/figures;
- a concise analysis runbook.

Aggregate outputs must still be reviewed for disclosure risk before external sharing.

## Manuscript framing supported by the freeze

The Methods section can now describe the audited ETL as a fixed methodological foundation: mapping policy, ambiguity handling, domain routing, exclusions, lineage/reconciliation, vocabulary validation, clean-build protocol, and semantic review.

The Results should explicitly separate two questions:

1. **Did the audited ETL behave as specified?** The final frozen rebuild answers this with matched reconciliation and zero hard/unexplained blockers.
2. **How much do clinically meaningful results differ between PCORnet and OMOP after using a validated ETL?** The publication-analysis stages will answer this independently of further ETL tuning.

That separation is central to the study: it prevents defects in a historical converter from being mistaken for intrinsic differences between PCORnet and OMOP.