# Conversion-code reconciliation of observed PCORnet–OMOP differences

This note reconciles the first two profiling runs with the supplied PCORnet-to-OMOP conversion code. The goal is to separate expected ETL behavior from unexplained divergence.

## 1. Drug exposure inflation is explained by three PCORnet source tables

The OMOP `drug_exposure` table contains 49,734,066 rows. The conversion SQL inserts three PCORnet domains into this table and assigns a distinct `drug_type_concept_id` to each source:

- `DISPENSING` -> `drug_type_concept_id = 32825`
- `MED_ADMIN` -> `drug_type_concept_id = 32830`
- `PRESCRIBING` -> `drug_type_concept_id = 32838`

The trace output exactly partitions the OMOP table:

| ETL source | OMOP rows |
|---|---:|
| PRESCRIBING | 34,438,450 |
| MED_ADMIN | 8,999,942 |
| DISPENSING | 6,295,674 |
| **Total** | **49,734,066** |

The returned PCORnet parquet set contains MED_ADMIN and DISPENSING but does not contain PRESCRIBING. Therefore the apparent 3.2-fold inflation is not an OMOP duplication problem. It is a source-coverage mismatch between the two exported folders.

The mapping loss is source-specific. `drug_concept_id = 0` occurs in 12,787,970 PRESCRIBING rows, 6,705,006 MED_ADMIN rows, and 165,415 DISPENSING rows. The SQL explains why these pathways differ. PRESCRIBING uses `RXNORM_CUI` and attempts a direct RxNorm concept lookup. DISPENSING uses NDC then a `Maps to` relationship. MED_ADMIN accepts NDC or RxNorm and then uses `Maps to` when possible. These pathways should be evaluated separately rather than pooling all drug records.

## 2. Observation inflation is primarily missing OBS_CLIN source data

The OMOP `observation` table contains 34,049,958 rows, while the returned `PCORnet_OBS_GEN.parquet` contains 590,662 rows.

The conversion package contains separate SQL for both `OBS_GEN` and `OBS_CLIN`, each inserting into OMOP `observation`. The PCORnet extraction package also contains an `obs_clin.ipynb`, but `PCORnet_OBS_CLIN.parquet` was not included in the comparison folder.

The difference is:

`34,049,958 - 590,662 = 33,459,296`

This is consistent with a large OBS_CLIN contribution. The current PCORnet export therefore cannot support a source-equivalent observation comparison until OBS_CLIN is exported.

The SQL also contains a potentially meaningful transformation: `OBSCLIN_RESULT_TEXT` is truncated with `LEFT(OBSCLIN_RESULT_TEXT, 60)`. This is a real information-loss mechanism that should be quantified once the OBS_CLIN source parquet is available.

## 3. Diagnosis-to-condition transformation is row-preserving but replaces missing dates

The conversion SQL inserts only `PCORnet.dbo.DIAGNOSIS` into OMOP `condition_occurrence` in the supplied conversion package. This agrees exactly with the profiling result:

- PCORnet DIAGNOSIS: 13,074,073 rows
- OMOP condition_occurrence: 13,074,073 rows

The SQL replaces null `DX_DATE` values with `1900-01-01`. The PCORnet profile contains 3,117,924 null DX_DATE values, and the OMOP table contains exactly 3,117,924 sentinel condition dates. Therefore this discrepancy is fully explained by ETL logic rather than data corruption.

This transformation is analytically important. Missing clinical dates become apparently valid historical dates, so cohort time-window logic can change unless the sentinel is explicitly recoded to missing.

The PCORnet `CONDITION` table contains 984,347 rows, but no supplied SQL transforms it into OMOP `condition_occurrence`. The current OMOP condition table therefore appears to represent DIAGNOSIS only. This is a potential source-domain omission that needs explicit treatment in the validation study.

## 4. Encounter end-date discrepancy is fully explained by sentinel replacement

The encounter conversion replaces missing discharge/end dates with `1900-01-01`.

- PCORnet ENCOUNTER null DISCHARGE_DATE: 2,283
- OMOP visit end dates equal to 1900-01-01: 2,283

The counts match exactly. This should be treated as a known transformation, not unexplained disagreement.

## 5. Provider loss is likely caused by a missing or unexecuted provider source load

The conversion code contains `Provider-Provider.sql` and many downstream scripts left-join to OMOP `provider` using PCORnet provider source identifiers. However, the returned PCORnet folder has no `PCORnet_PROVIDER.parquet`, and the OMOP `provider.parquet` contains zero rows.

As a result, provider foreign keys are null in downstream OMOP tables even when the PCORnet source domain contains provider identifiers. Provider-level analyses cannot be considered equivalent under the current conversion output.

This is a high-priority ETL completeness issue rather than an inherent PCORnet-versus-OMOP modeling difference.

## 6. Procedure occurrence cannot yet be reconciled from the supplied conversion SQL

OMOP `procedure_occurrence` contains 11,657,146 rows. The PCORnet extraction package contains `Procedure.ipynb`, which loads a PCORnet `procedures` table, but the returned PCORnet parquet folder has no `PCORnet_PROCEDURES.parquet`.

More importantly, the supplied `ConversionScripts.zip` contains no procedure-to-`procedure_occurrence` SQL file. Therefore the observed OMOP procedure transformation cannot yet be audited from the supplied conversion SQL alone.

The OMOP table contains two procedure type concepts:

- 32817: 10,116,327 rows
- 32821: 1,540,819 rows

But the exact source-code logic assigning these types is not present in the supplied conversion scripts. We should not infer it without the missing procedure conversion code.

## 7. The supplied VITAL conversion file appears erroneous

`Vital-Obs-Measurement.sql` is byte-for-byte identical to `Prescribing.sql` in the supplied conversion package. Both files have the same SHA-256 hash:

`f5da8a63e1f5a25fcf20f954abd1e8f2503b39a972700a310819492ba62b3304`

The file named for VITAL actually inserts PRESCRIBING fields into `drug_exposure`. This appears to be a packaging or source-control error. We cannot use this file to determine how PCORnet VITAL data were transformed.

This matters because the PCORnet extraction package contains `Vital.ipynb`, but the returned PCORnet parquet set does not contain VITAL. We need the actual VITAL-to-OMOP conversion script before making claims about vital-sign preservation.

## 8. Visit linkage behavior is source-dependent and follows the SQL

All diagnoses, measurements, observations, and procedures in the OMOP export are linked to visits. Drug exposure is only 62.0% linked overall because the conversion pathways differ:

- MED_ADMIN joins to encounter and has complete visit linkage in the returned OMOP data.
- DISPENSING has no visit field in its conversion SQL, so its OMOP visit linkage is null by design.
- PRESCRIBING joins through ENCOUNTERID when available, leaving some rows unlinked.

This should be considered expected ETL behavior, not a generic OMOP quality defect.

## 9. Required source additions before formal CDM equivalence testing

The current PCORnet parquet export should be expanded to include at least:

- PRESCRIBING
- PROCEDURES
- OBS_CLIN
- VITAL
- PROVIDER

Without these tables, several apparent PCORnet-versus-OMOP differences are simply differences in source coverage.

## 10. Implications for the validation paper

The validation should classify discrepancies into four groups:

1. **Source-coverage mismatch**: source table used by ETL but absent from the PCORnet comparison export.
2. **Intentional structural transformation**: one source domain mapped into a different OMOP domain or several source domains combined.
3. **Intentional value transformation**: null dates replaced by sentinel values, text truncation, manual categorical mappings, vocabulary standardization.
4. **Potential ETL defect or information loss**: missing provider population, absent CONDITION transformation, missing procedure conversion code, erroneous VITAL script, or failed vocabulary mapping.

This classification should be resolved before phenotype or statistical equivalence analyses. Otherwise we would attribute implementation-specific ETL behavior to the CDM itself.
