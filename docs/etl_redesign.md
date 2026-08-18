# PCORnet to OMOP ETL redesign

## Objective

Build a reproducible, auditable PCORnet-to-OMOP CDM v5.4 ETL that can be executed with one primary command and validated against the source PCORnet data.

The ETL will not treat the prior Ajith scripts as authoritative. They are useful historical/reference material, but the implementation will be checked against the OMOP CDM specification, standardized vocabulary conventions, observed PCORnet schemas, and source-to-target validation results.

## External references

Primary specification sources:

- OHDSI CommonDataModel repository and OMOP CDM v5.4 release artifacts.
- OHDSI Standardized Vocabularies distributed through Athena.
- OHDSI DataQualityDashboard for post-ETL CDM validation.
- OHDSI WhiteRabbit/RabbitInAHat methodology as ETL-design reference.

Historical implementation reference:

- `yuey11/PCORnet2OMOP_ETL_tool`, which contains 17 PCORnet domain scripts including PROCEDURES, VITAL, CONDITION, and IMMUNIZATION. This repository is treated as reference material, not copied as the normative implementation.
- Ajith's conversion package used in the prior pregnancy project.

## Design principles

1. **Pinned target model.** Target OMOP CDM v5.4.2 unless a later deliberate migration is approved.
2. **Reproducible vocabulary.** Record Athena vocabulary release/version and required vocabularies in ETL metadata.
3. **No silent defaults.** Do not convert missing dates to artificial values such as `1900-01-01`. Preserve missingness where OMOP permits it; when OMOP requires a value, apply a documented rule and quantify affected records.
4. **No silent row multiplication.** Each domain transformation must report source rows, emitted OMOP rows, excluded rows, and reasons for one-to-many mappings.
5. **Idempotent execution.** Re-running an ETL stage must not duplicate records. Each stage either rebuilds its target domain or uses explicit run/version semantics.
6. **Preserve lineage.** Retain source values and source concept IDs where supported. Produce aggregate source-to-target reconciliation for every domain.
7. **Explicit vocabulary mapping.** Separate source concept lookup from mapping to standard concepts. Quantify unmapped source codes and standard concept ID 0 records.
8. **Dependency-aware orchestration.** Run person and visit/care-site/location stages before dependent clinical fact tables.
9. **Fail fast.** Preflight checks must stop execution for missing required source tables, missing vocabulary tables, schema mismatch, or duplicate stage configuration.
10. **Safe outputs.** Validation artifacts contain aggregate data with small-cell suppression where needed.

## Proposed user workflow

The intended final interface is one main command:

```bash
pcornet-omop-etl run --config config/etl.yaml
```

The runner will execute these stages:

1. Preflight source and environment checks.
2. Create or verify the OMOP v5.4 schema.
3. Load/verify Athena vocabulary tables.
4. Load PCORnet parquet files into staging tables.
5. Run deterministic domain transformations in dependency order.
6. Populate `cdm_source` with ETL and vocabulary provenance.
7. Export selected OMOP tables to parquet when configured.
8. Run source-to-target reconciliation.
9. Optionally run OHDSI DataQualityDashboard and save its results.

Advanced users will also be able to execute an individual stage/domain for debugging.

## Initial domain scope

The current source data support at least:

- DEMOGRAPHIC -> person
- ENROLLMENT -> observation_period
- ENCOUNTER -> visit_occurrence, care_site, location
- DIAGNOSIS -> condition_occurrence
- CONDITION -> condition_occurrence or an explicitly justified alternative representation
- PROCEDURES -> procedure_occurrence
- VITAL -> measurement and observation
- LAB_RESULT_CM -> measurement
- PRESCRIBING -> drug_exposure
- DISPENSING -> drug_exposure
- MED_ADMIN -> drug_exposure
- IMMUNIZATION -> drug_exposure/procedure representation according to OMOP conventions
- OBS_CLIN -> observation and/or domain-routed target based on standard concept domain
- OBS_GEN -> observation and/or domain-routed target based on standard concept domain
- DEATH / DEATH_CAUSE -> death
- LDS_ADDRESS_HISTORY -> location
- PROVIDER -> provider when source data exist

## Known defects in the prior build to prevent

The validation work already identified the following implementation problems in the prior OMOP build:

- PRESCRIBING was inserted twice: 17,219,225 PCORnet records produced exactly 34,438,450 OMOP records attributed to the prescribing pathway.
- The supplied `Vital-Obs-Measurement.sql` was identical to `Prescribing.sql`; VITAL did not contribute to the resulting measurement/observation tables.
- CONDITION was present in PCORnet but absent from the observed OMOP condition table.
- IMMUNIZATION was present in PCORnet but absent from the observed OMOP drug table.
- Missing source dates were sometimes replaced with `1900-01-01`, changing missingness into an artificial historical date.
- The supplied local conversion package omitted the procedure transformation even though the older public reference implementation contains one.

These are ETL implementation effects, not properties of OMOP itself.

## Validation strategy

Every domain ETL will produce a machine-readable reconciliation record with at least:

- source row count
- source patients
- target row count
- target patients
- excluded source rows and reason
- rows emitted more than once and reason
- source code mapping rate
- standard concept mapping rate
- visit linkage rate
- missing date/value preservation
- invalid/default concept frequency

After structural validation, the project will proceed to patient-level semantic concordance, phenotype reproducibility, and analytical equivalence comparisons.

## Development sequence

Phase 1: establish the orchestration/preflight framework and pin external specifications.

Phase 2: reimplement and unit-test core low-complexity domains: person, observation_period, visit_occurrence, death.

Phase 3: implement vocabulary-dependent clinical domains: condition, procedure, measurement, observation, drug exposure, immunization.

Phase 4: run the new ETL on the full PCORnet parquet set and compare it with both PCORnet and the prior OMOP build.

Phase 5: run OHDSI DataQualityDashboard and freeze a validated ETL release for the scientific comparison study.
