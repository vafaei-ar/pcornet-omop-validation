# Current status and publication plan

_Last updated: 2026-08-26_

## Purpose

This document records the state of the audited PCORnet-to-OMOP ETL, what has been completed, what the current clean rebuild demonstrates, and the remaining work required before publication analyses and manuscript submission.

The central methodological goal is to separate true Common Data Model representation effects from ETL implementation effects. The ETL therefore aims to be source-derived, vocabulary-driven, OMOP-conformant, reproducible, and auditable rather than tuned to reproduce a prior OMOP database or a particular row count.

## What has been completed

### 1. ETL redesign and validation architecture

The original conversion behavior was replaced or hardened where needed so that important decisions are explicit and auditable.

Key principles now implemented include:

- source and target schemas are configurable rather than assumed;
- required-date rules are explicit and exclusions are quantified;
- exact source-vocabulary mappings do not choose an arbitrary `TOP(1)` concept;
- ambiguous mappings fail closed to concept `0` unless a defensible unique Standard mapping exists;
- nonzero target concepts are validated against active OMOP vocabulary records and the expected domain where appropriate;
- route ledgers and lineage/xwalk tables are retained so that source events can be reconciled to target events;
- cross-domain mappings are materialized according to vocabulary semantics rather than forced into the source table's nominal OMOP analogue;
- unresolved or semantically unsupported mappings remain transparent through concept `0` and review flags instead of being hidden through imputation;
- clean reset/rebuild logic operates only on the configured target and does not search for or modify the prior/comparator OMOP database.

### 2. Major domain-specific hardening

The following areas received substantial redesign or validation work.

#### Person

PCORnet gender, race, and ethnicity mappings are validated against active Standard concepts in the expected OMOP domains. Deprecated/non-Standard candidate concepts are rejected to concept `0` while preserving the source value. This fixed the prior freeze blocker caused by deprecated race concepts.

#### Visit occurrence

PCORnet encounter types are mapped conservatively. Numeric visit-time fields were empirically profiled and validated as SAS seconds within a day. The final audit confirms zero materialized datetime mismatches under that interpretation.

#### Procedure routing

PCORnet PROCEDURES events are routed through a target-schema event ledger. One-to-many mappings are retained. Event-domain targets are materialized in their corresponding OMOP domains, while non-event semantic components remain represented in the route ledger rather than being incorrectly emitted as standalone events.

#### OBS_CLIN

OBS_CLIN is routed by validated Standard concept domain into Measurement, Observation, or Condition. Ambiguous/unresolved mappings are not forced into Measurement. This preserves domain semantics and makes unresolved rows explicit.

#### Condition

DIAGNOSIS and CONDITION were redesigned around a canonical event-route model. Multi-target `Maps to` behavior is supported; `Maps to value` and other non-event semantic components do not independently create events. Source events with only non-event targets receive a Condition concept-0 fallback so that the source event remains represented and auditable.

Condition type and status mappings were also hardened. Only active Standard concepts in the correct OMOP domains are retained; previously problematic non-Standard status concepts are now rejected to `0`. This removed the prior Condition status freeze blocker.

#### Measurement

LAB, VITAL, Procedure-derived Measurement, and OBS_CLIN Measurement rows are reconciled with lineage. LOINC/UCUM mapping is source-derived, and exact UCUM matching is case-sensitive. Unsupported or ambiguous units remain concept `0` rather than being guessed.

#### Observation

Observation materialization combines routed OBS_CLIN, OBS_GEN, LAB, Procedure, and VITAL components with source-specific lineage and reconciliation.

#### Drug exposure

Drug routes are built from PRESCRIBING, DISPENSING, MED_ADMIN, IMMUNIZATION, and Procedure-derived Drug routes. RxNorm/NDC mapping is vocabulary-driven and supports one-to-many mappings. Route finalization maps only uniquely supported active Standard Route concepts; unresolved standardized route values remain concept `0` and are reviewed explicitly.

#### Device and Specimen

Procedure-derived and Condition cross-domain Device/Specimen routes are materialized with dedicated lineage and reconciliation.

#### Death

Death materialization is conservative. Required fields are enforced, missing-date sentinels are not invented, and the current source has no DEATH_CAUSE rows. Unsupported death type/cause semantics remain concept `0` under an explicit provenance policy.

### 3. Clean rebuild and phase-by-phase validation

A guarded reset was executed against the isolated target database `OMOP_VALIDATED_A`. The prior/comparator OMOP database was not modified.

The clean build was then executed in ordered phases:

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
12. Global and semantic validation
13. Explicit review decisions
14. Freeze-candidate manifest

All materialization phases reported matched source/route/target/lineage reconciliation for the components they own.

## Current clean-build result

The Phase 12 global reconciliation produced the following target counts:

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

These counts are outputs of the current source and vocabulary semantics, not hard-coded acceptance thresholds.

The important acceptance results are:

- global reconciliation: **matched**;
- visit-time semantics: **matched**;
- materialized visit datetime mismatches: **0**;
- duplicate primary-key groups reported by the validation phase: **0**;
- reversed intervals in the audited interval-bearing tables: **0**;
- semantic hard blockers: **none**;
- semantic review flags: **10**;
- explained review flags: **10**;
- unexplained review flags: **0**;
- auxiliary concept blockers in Phase 14: **none**.

The remaining semantic review flags are explicit concept-0/provenance decisions, including type-concept zeros, unresolved drug route concepts, and the explicit Death provenance policy. They are not hidden reconciliation failures.

## Important interpretation of the current Phase 14 result

Phase 14 successfully produced a freeze-candidate manifest and reported:

- `phase13_status: freeze_candidate_reviewed`
- `visit_time_semantics_status: matched`
- `auxiliary_concept_blockers: []`
- build-time `git_head: ee7e876ccf0541aeb0512de5a726a70b1893d61f`
- `dirty_worktree_entries: 5`

Therefore, the current database is a successful **semantic-validation rebuild**, but it should not yet be treated as the final publication freeze. The working tree was not clean at manifest time, and a small number of source-code robustness/documentation issues still need to be resolved before one final build is executed from an exact frozen commit.

## Remaining engineering work before the final freeze

The remaining work is intentionally narrow and should avoid changing domain semantics unless a new validation defect is found.

### Required cleanup

1. Fix the known Person existing-target idempotence robustness bug in the validation path. A multi-row `UNION ALL` count query is currently consumed through a scalar helper and can fail when the Person target already exists. This did not affect the clean empty-target build, but it should be corrected before release.
2. Update the canonical/reported stage order so that the visit-time semantics audit and final manifest are represented accurately.
3. Review remaining audit/preflight/readiness code for hard-coded `dbo` assumptions and use the configured schema consistently where needed.
4. Review the freeze-manifest provenance fields, source/config hashing, and documentation for reproducibility completeness.
5. Clean the worktree and commit all intended ETL, audit, and documentation changes.

### Final code-frozen rebuild

After the cleanup commit is complete:

1. record the exact Git commit intended for publication;
2. run guarded reset against the isolated validated target only;
3. rerun the full clean build from zero using the frozen commit and publication configuration;
4. rerun Phases 12-14 and require:
   - matched global reconciliation,
   - zero hard semantic blockers,
   - zero unexplained review flags,
   - zero auxiliary concept blockers,
   - clean worktree in the final manifest;
5. archive the final audit bundle and manifest as the reproducibility record.

No row count from the previous or current build should be used as an acceptance threshold. The final build is accepted on reconciliation, required-field/interval integrity, vocabulary-semantic validity, explicit ambiguity/exclusion policy, and documented review decisions.

## Publication analysis plan

Once the ETL is frozen, the project moves from ETL validation to the scientific PCORnet-versus-OMOP comparison.

### Stage A: Structural and semantic concordance

Quantify what is preserved, transformed, expanded, routed to another OMOP domain, or unresolved during conversion. Candidate publication outputs include:

- source-event eligibility and quantified exclusions;
- source-to-route-to-target reconciliation by source table and target domain;
- one-to-many mapping expansion;
- concept-0 rates by domain and mapping reason;
- visit linkage rates;
- route and unit mapping coverage;
- cross-domain routing patterns;
- vocabulary mapping validity and ambiguity summaries.

### Stage B: Patient-level semantic concordance

Define comparable patient-level clinical variables in PCORnet and the frozen OMOP build and measure agreement. Examples include diagnoses, procedures, medications, measurements, encounters, and selected temporal features. Analyses should distinguish direct representation differences from information loss.

### Stage C: Phenotype reproducibility

Implement selected computable phenotypes independently in PCORnet and OMOP, then compare:

- cohort size;
- patient overlap;
- positive/negative agreement;
- sources of discordance;
- sensitivity to vocabulary/domain representation choices.

Phenotypes should be chosen to exercise different domains and ETL behaviors rather than only cases expected to agree.

### Stage D: Analytical equivalence

Run matched downstream analyses in both CDMs and compare estimates, uncertainty, and conclusions. Depending on the final study design, this may include descriptive rates, associations, time-to-event analyses, or other pre-specified models. Sensitivity analyses should connect observed analytic differences back to documented ETL/CDM representation differences.

## Reproducibility bundle for the paper

The publication bundle should preserve, without committing protected data:

- frozen Git commit SHA;
- publication ETL configuration with secrets removed;
- source file inventory and hashes where permissible;
- vocabulary version/provenance information;
- ETL source-file hashes;
- phase audit JSON files;
- route/lineage reconciliation summaries;
- exclusion summaries;
- semantic review decisions;
- final freeze manifest;
- scripts that generate manuscript tables and figures;
- a concise runbook describing how to reproduce the clean build and analyses.

Aggregate outputs should still be reviewed for disclosure risk before being shared outside the approved research environment.

## Manuscript structure supported by this work

The current ETL and audit trail directly support a Methods section describing:

- source and target CDMs;
- vocabulary and mapping policy;
- domain routing logic;
- ambiguity and concept-0 policy;
- required-date and exclusion rules;
- lineage and reconciliation framework;
- clean-build validation protocol;
- semantic review process.

The Results section can then separate two questions:

1. **Did the audited ETL behave as specified?** This is supported by the clean-build reconciliation and semantic validation.
2. **How much do clinically meaningful results differ between PCORnet and OMOP after using a validated ETL?** This will be answered by the concordance, phenotype, and analytical-equivalence stages.

That separation is central to the publication: it prevents implementation defects in a historical converter from being mistaken for intrinsic differences between the PCORnet and OMOP data models.
