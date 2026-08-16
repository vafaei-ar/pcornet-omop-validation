# Notes from supplied PCORnet-to-OMOP ETL package

The supplied package documents a staged conversion from PCORnet parquet data to an OMOP schema. The conversion SQL links PCORnet patients to OMOP through `PATID = person.person_source_value` and assigns OMOP surrogate identifiers. Encounter-linked domains use an encounter-to-visit mapping table. Several clinical domains are inserted first and then updated using OMOP vocabulary tables.

Observed intended transformations include:

- DEMOGRAPHIC -> PERSON.
- ENROLLMENT -> OBSERVATION_PERIOD.
- ENCOUNTER -> VISIT_OCCURRENCE plus LOCATION and CARE_SITE.
- DIAGNOSIS -> CONDITION_OCCURRENCE, with source concept lookup and `Maps to` standard concept mapping.
- DISPENSING -> DRUG_EXPOSURE, using NDC source concepts followed by standard mapping.
- LAB_RESULT_CM -> MEASUREMENT, using LOINC and explicit unit mappings.
- MED_ADMIN and prescribing logic also feed DRUG_EXPOSURE.
- OBS_GEN and OBS_CLIN feed OBSERVATION.

These relationships mean that domain-level row equality is not a valid universal fidelity criterion. Validation should preserve source provenance and explicitly test patient linkage, visit linkage, source-code retention, source-to-standard concept mapping, and transformation multiplicity.

The documentation also recommends post-run row-count checks and patient-timeline spot checks. This project extends that basic QA into reproducible semantic, phenotype, and analytical validation.
