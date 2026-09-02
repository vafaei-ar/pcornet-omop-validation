# Full-source rerun reconciliation

This note records findings from the rerun after adding the previously missing PCORnet source parquet files.

## High-confidence findings

### PRESCRIBING duplicated exactly twice

PCORnet `PRESCRIBING` contains 17,219,225 rows. OMOP `drug_exposure` contains 34,438,450 rows with `drug_type_concept_id = 32838`, exactly 2 × the source row count.

The conversion package contains `Prescribing.sql` and a file named `Vital-Obs-Measurement.sql` that is byte-for-byte identical to `Prescribing.sql` (same SHA-256). If both scripts were executed in the documented sequence, PRESCRIBING would be inserted twice into `drug_exposure`. This exactly matches the observed counts.

This is an ETL implementation defect, not a CDM structural difference.

### VITAL not converted

PCORnet `VITAL` contains 5,952,127 rows. OMOP `measurement` contains exactly 19,838,905 rows, which equals the PCORnet `LAB_RESULT_CM` row count. OMOP `observation` contains exactly 33,459,296 `OBS_CLIN` rows plus 590,662 `OBS_GEN` rows. Therefore no OMOP rows are attributable to VITAL.

The supplied `Vital-Obs-Measurement.sql` is actually a duplicate of the prescribing script, which explains the absence of VITAL conversion.

This is an ETL implementation defect and may materially affect analyses using blood pressure, height, weight, BMI, smoking, or tobacco variables.

### PROCEDURES loses rows with missing PX_DATE

PCORnet `PROCEDURES` contains 11,685,632 rows. OMOP `procedure_occurrence` contains 11,657,146 rows. The difference is exactly 28,486 rows, which equals the number of PCORnet procedure rows with null `PX_DATE`.

This strongly indicates that procedures without a procedure date were excluded during conversion. Three patients present in PCORnet procedures are absent from OMOP procedure_occurrence after this filtering.

The supplied conversion package does not include the procedure conversion script, so the exact WHERE/JOIN condition still requires confirmation from the original ETL source.

### OBS_CLIN and OBS_GEN reconcile exactly

PCORnet `OBS_CLIN` = 33,459,296 rows and `OBS_GEN` = 590,662 rows. Their sum equals OMOP `observation` = 34,049,958 rows exactly. OMOP observation type concept IDs also separate these two pathways exactly.

This is strong evidence of row preservation at the table-count level for these domains.

### CONDITION omitted

PCORnet `CONDITION` contains 984,347 rows. OMOP `condition_occurrence` contains exactly 13,074,073 rows, identical to PCORnet `DIAGNOSIS`. The supplied ETL package includes DIAGNOSIS-to-condition_occurrence conversion but no CONDITION conversion.

Thus the separate PCORnet CONDITION domain appears omitted from OMOP in this implementation.

### IMMUNIZATION omitted

PCORnet `IMMUNIZATION` contains 352,313 rows. OMOP `drug_exposure` is fully explained by 2 × PRESCRIBING + MED_ADMIN + DISPENSING, with no residual rows attributable to immunization. No immunization conversion script is present in the supplied package.

Thus immunization information appears omitted from this OMOP build.

### Provider absence is source-related

The PCORnet PROVIDER source table is absent. OMOP provider is empty. This is expected given the source extraction and should not be classified as an OMOP conversion failure.

## Expected transformations requiring downstream handling

- Null DIAGNOSIS dates are converted to `1900-01-01`: 3,117,924 rows, exactly matching the OMOP sentinel count.
- Null encounter end dates are converted to `1900-01-01`: 2,283 rows, exactly matching the OMOP visit sentinel count.
- PRESCRIBING missing start/end dates are similarly converted to 1900-01-01. Because PRESCRIBING was duplicated, the sentinel counts are also duplicated.
- DISPENSING has no visit link by design in this ETL pathway.
- OBS_CLIN result text is truncated to 60 characters in the supplied SQL.

## Vocabulary mapping

Standard concept mapping is excellent for LAB_RESULT_CM/measurement, but weaker for several other domains:

- condition_occurrence: 27,693 rows with standard concept 0 (0.21%)
- observation: 302,161 rows with standard concept 0 (0.89%)
- procedure_occurrence: 576,258 rows with standard concept 0 (4.94%)
- drug_exposure: 19,658,391 rows with standard concept 0 (39.5%), heavily driven by prescribing and medication administration pathways

These rates should be analyzed by source vocabulary and code class rather than interpreted as one pooled mapping failure rate.

## Implication for the validation study

The current OMOP dataset contains both legitimate CDM transformations and specific implementation defects. Before testing analytical equivalence, analyses should either correct the OMOP build or explicitly account for these defects. Otherwise any observed PCORnet-versus-OMOP difference may reflect the implementation rather than the common data model itself.
