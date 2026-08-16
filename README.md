# PCORnet to OMOP validation

This repository develops a reproducible validation framework for evaluating whether transformation from the PCORnet Common Data Model to the OMOP Common Data Model preserves clinically and analytically relevant information.

The project does **not** assume that tables or row counts should be identical across CDMs. The ETL changes table structure, identifiers, visit representation, and vocabulary concepts. Validation therefore proceeds from data understanding to semantic concordance, phenotype reproducibility, and finally analytical equivalence.

## Current phase: data understanding

The first executable profiles both parquet datasets without exporting patient-level rows. It measures table structure, missingness, candidate-key behavior, date coverage, records per patient, patient crosswalk coverage, OMOP concept ID 0 frequency, and source-to-standard concept mapping. Low-frequency categorical values are suppressed by default when `n < 11`.

Expected local layout:

```text
/usr/local/datasets/OMOP/
├── OMOP_parquet/
└── PCORnet_parquet/
```

## Installation

```bash
cd pcornet-omop-validation
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For development/tests:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Run the initial profile

```bash
pcornet-omop-profile \
  --pcornet /usr/local/datasets/OMOP/PCORnet_parquet \
  --omop /usr/local/datasets/OMOP/OMOP_parquet \
  --output results
```

Or:

```bash
cp config/example.yaml config/local.yaml
pcornet-omop-profile --config config/local.yaml
```

## Run the transformation trace

After the initial profile, run the targeted ETL trace. It checks whether expected PCORnet source domains are present, attributes OMOP drug records to dispensing/medication-administration/prescribing ETL paths, profiles OMOP type concepts, counts the ETL sentinel date `1900-01-01`, and measures visit linkage.

```bash
python scripts/02_transform_trace.py \
  --pcornet /usr/local/datasets/OMOP/PCORnet_parquet \
  --omop /usr/local/datasets/OMOP/OMOP_parquet \
  --output results/transform_trace
```

The `results/` directory is ignored by Git. Zip it after the run and return that ZIP for review.

```bash
zip -r pcornet_omop_profile_results.zip results/
```

## Initial result files

- `inventory.csv`: rows, columns, patient identifier, distinct patients, exact duplicates, and file size.
- `column_profiles.csv`: null and distinct counts for every column.
- `date_ranges.csv`: observed minimum and maximum for date/time-like columns.
- `key_profiles.csv`: uniqueness checks for identifier-like columns.
- `person_crosswalk_summary.csv`: aggregate matching of PCORnet `PATID` to OMOP `person_source_value`.
- `omop_concept_zero_profile.csv`: frequency of unmapped/unknown standard concept IDs.
- `omop_source_standard_mapping.csv`: source-concept and standard-concept population.
- `table_profiles/`: records-per-patient quantiles.
- `categorical_profiles/`: top categorical values with small-cell suppression.
- `transform_trace/pcornet_source_file_coverage.csv`: whether PCORnet source domains needed for a source-equivalent comparison are available.
- `transform_trace/drug_exposure_etl_source_trace.csv`: OMOP drug records stratified by the ETL source type concept IDs.
- `transform_trace/omop_type_concept_counts.csv`: counts of OMOP `*_type_concept_id` fields.
- `transform_trace/omop_sentinel_date_counts.csv`: frequency of the ETL default date `1900-01-01`.
- `transform_trace/omop_visit_linkage.csv`: visit linkage rates for OMOP clinical domains.
- `run_metadata.json`: software and run provenance.

## Planned validation stages

1. ETL and structural fidelity.
2. Patient-level semantic concordance.
3. Phenotype reproducibility.
4. Analytical equivalence and sensitivity analyses.

See `docs/data_understanding.md` and `docs/etl_notes.md` for the current scientific framing.

## Data governance

Do not commit source parquet files or returned result bundles. Aggregate outputs can still contain sensitive information. Review outputs before sharing outside the approved research environment. The repository is currently public, so only code and non-sensitive documentation should be committed.
