# Data-understanding phase

The first phase is intentionally descriptive. It should establish what information is present in each CDM and how the ETL changes its representation before formal equivalence testing begins.

## Questions to answer

1. Are the same patients represented in PCORnet and OMOP?
2. Can PCORnet `PATID` be matched deterministically to OMOP `person_source_value`?
3. Which PCORnet domains feed each OMOP domain?
4. Which identifiers preserve linkage across person, encounter/visit, provider, condition, drug, measurement, and observation records?
5. How much source vocabulary information survives mapping to OMOP concepts?
6. Where does OMOP use concept ID 0, and is that expected from source coding or caused by ETL loss?
7. Do record counts differ because of intentional many-to-one or one-to-many transformations?
8. Are dates, missingness, and records-per-person distributions preserved closely enough for later phenotype and outcome analyses?

## Initial outputs

The profiler writes aggregate CSV files only. Raw patient identifiers and raw rows are not exported. Low-frequency categorical values are replaced with `<SUPPRESSED>` using a configurable minimum cell size, default 11.

The first returned results bundle should include `inventory.csv`, `column_profiles.csv`, `date_ranges.csv`, `key_profiles.csv`, `person_crosswalk_summary.csv`, `omop_concept_zero_profile.csv`, `omop_source_standard_mapping.csv`, `intended_domain_map.csv`, per-table records-per-patient summaries, categorical summaries, and `run_metadata.json`.

## Interpretation rule

Do not treat unequal PCORnet and OMOP row counts as ETL failure without understanding transformation grain. The main study will later distinguish expected CDM transformation, vocabulary mapping loss, implementation error, and unexplained information loss.
