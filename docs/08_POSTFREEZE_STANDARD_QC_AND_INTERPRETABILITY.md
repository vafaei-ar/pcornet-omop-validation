# Post-freeze standard OMOP QC and interpretability extension (proposed)

**State: CODE/PREPARATION ONLY, NOT EXECUTED.** This extension is separate from the original frozen Stage A–E study and does not revise the approved primary estimates, database, phenotype, or outcome. All clinical row-level data remain within the authorized institutional environment.

- Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.
- Reference publication branch SHA: `1c271c37a9d1a279f075a8edee439fe0cce7706f`.
- Definition: `study_definitions/postfreeze_standard_qc_interpretability_v1.json`.
- R runner: `scripts/run_postfreeze_omop_qc.R`.
- Python interpretability engine: `src/pcornet_omop_validation/study/postfreeze_model_interpretability.py`.
- Synthetic model tests: `tests/test_postfreeze_interpretability.py`.

## A. OHDSI DQD and Achilles

The R packages support OMOP **v5.4**. The frozen target is OMOP v5.4.2, so use `cdmVersion = "5.4"`. See upstream official documentation: <https://ohdsi.github.io/DataQualityDashboard/reference/executeDqChecks.html>, <https://ohdsi.github.io/Achilles/reference/achilles.html>.

**Preparation by the local database/R administrator**:

1. Confirm that the configured server/database holds the exact **frozen publication target**, not another OMOP build. Record source snapshot and database identity privately.
2. Install and review R (>=4), Java, a local SQL Server JDBC driver and OHDSI `DatabaseConnector`, `DataQualityDashboard`, `Achilles`, `jsonlite`. Use institution-approved package versions.
3. Provision a dedicated **results** schema and separate **scratch** schema writable by the QC account. Neither may be the frozen target schema. DQD runs with `writeToTable=FALSE`; Achilles writes to the results/scratch schemas only.
4. Confirm expected duration/resource impact with database administration. DQD can run thousands of checks. Achilles can execute substantial queries. Do not execute during ETL or competing heavy jobs.
5. Set environment variables locally (never commit values):

    `OHDSI_DB_SERVER` = SQL Server connection endpoint including database as required by DatabaseConnector  
    `OHDSI_DB_USER` and `OHDSI_DB_PASSWORD` = institution-authorized credentials  
    `OHDSI_JDBC_DRIVER_PATH` = local JDBC driver directory  
    `OHDSI_CDM_SCHEMA` = `database.schema` containing frozen OMOP  
    `OHDSI_RESULTS_SCHEMA` = `database.schema` for QC results  
    `OHDSI_SCRATCH_SCHEMA` = `database.schema` for scratch work  
    `OHDSI_QC_EXPECTED_PACKAGE_VERSION` = verified installed version for whichever tool runs  
    `OHDSI_QC_CONFIRM` = `RUN_POSTFREEZE_QC_ON_FROZEN_TARGET`

Run separately after authorization:

```bash
Rscript scripts/run_postfreeze_omop_qc.R dqd
# Re-set OHDSI_QC_EXPECTED_PACKAGE_VERSION to the installed Achilles version.
Rscript scripts/run_postfreeze_omop_qc.R achilles
```

The runner checks aggregate frozen-target row counts before and after each tool. It writes a local execution manifest (including package version) under ignored `results/postfreeze_standard_qc/`. **This is not a proof that the target content is immutable**, so the SQL account must also have no update/delete privileges on the CDM schema.

**Reporting plan:** tally DQD checks by check level and severity, distinguishing passed / failed / not applicable / SQL execution error. Identify failing check name, target table/field, numerator/denominator and failure threshold when available, and explain each failure without silently changing defaults. Report Achilles as descriptive statistics, not additional DQD passes. Include concept-zero Drug Exposure coverage and diagnosis-date exclusions as separately measured source-aware signals. Do not conclude that QC passed or failed before actually running it.

The complete raw DQD JSON and Achilles tables/logs are **private** until institutional disclosure review; do not commit them. Small-cell suppression is enabled for Achilles at <=10. Aggregate DQD failures or characteristics may also require disclosure screening.

## B. Random Forest / XGBoost model attribution

**Input prerequisite, exporter implemented but NOT executed against institutional data:** four institutional local Parquet extracts with the **exact same six locked predictors**, a stable common patient key `patid`, `index_date` and the locked 90-day binary `outcome`. Only one row per patient. The extracts MUST be independently derived using the already completed Stage C/D/E definitions (including the provenance-aware target-date fallback overlay). Do not fill missing values or manufacture fallback records just to satisfy expected row counts.

| Local private input | Expected 90-day eligible / events | Required relationship |
| --- | ---: | --- |
| `pcornet_full` | 6,508 / 1,798 | Full source D0 with 90-day observability |
| `pcornet_shared` | 3,822 / 1,132 | Exact subset of full PCORnet, matching patient/episode/feature values |
| `omop_frozen` | 3,822 / 1,132 | Same patients, index dates and 90-day labels as shared PCORnet |
| `omop_fallback` | 6,508 / 1,798 | Same patients, index dates and 90-day labels as full PCORnet, using source-lineage-preserving encounter-date overlay |

**Private input exporter (explicit authorization required, not yet run):**

```bash
python -m pcornet_omop_validation.study.postfreeze_interpretability_extract \
  --config config/etl_A.yaml \
  --output-dir results/private_inputs \
  --confirm-private
```

This exporter re-executes the unchanged, anchor-checked Stage E analysis and the separate completed Stage D fallback sensitivity through narrowly scoped read hooks, capturing the native features while their SQL Server temporary tables exist. It does **not** modify those scripts. It requires the completed, locked Stage E preflight and Stage C fallback result in the original local `results/` tree. Any missing outputs, SQL changes, or failed anchor verification stop the export. The four row-level Parquet files are retained only under ignored `results/private_inputs/`; do not attach, commit, or publish them.

**Important:** The private exporter has not yet been exercised against the institution's frozen database and requires a supervised first run. Independent review must verify that the feature capture exactly preserves the locked feature definitions, especially the native prior-stroke code and fallback overlay. The model analysis engine itself intentionally **does not** query the clinical database or save patient-level rows. No model results can be claimed until that extraction is reviewed and verified. The original prior-ischemic-stroke feature requires a recorded diagnosis date, and the planned 5-feature sensitivity excludes it rather than silently redefining it.

Install local analysis dependencies in the existing environment: `pip install -e '.[analysis,interpretability]'`. Record package versions in the local run manifest before manuscript reporting.

Then run (paths below are schematic and must stay inside a protected, ignored local directory):

```bash
python -m pcornet_omop_validation.study.postfreeze_model_interpretability \
  --pcornet-full results/private_inputs/pcornet_full.parquet \
  --pcornet-shared results/private_inputs/pcornet_shared.parquet \
  --omop-frozen results/private_inputs/omop_frozen.parquet \
  --omop-fallback results/private_inputs/omop_fallback.parquet \
  --output results/postfreeze_interpretability/aggregate_results.json
```

The program rejects identity/index/outcome mismatches, nonbinary labels, duplicate patients, within-PCORnet feature changes under restriction, and incorrect population/event anchors **before fitting**. It then reuses the Stage E SHA-256 70/30 patient split and fits fixed-parameter Random Forest and XGBoost models, with and without the diagnosis-date-sensitive prior-stroke predictor.

**Interpretation levels:** (1) native held-out test performance/importance reflects each cohort; (2) the same shared held-out patients across all four models provides the cleaner matched-evaluation contrast. Permutation importance is held-out AUROC reduction (20 repeats). SHAP uses positive-class predicted-probability units with a deterministic common source training-background sample (200 rows). The importance sample is capped at 500. We must inspect predictor correlations, the prior-stroke exclusion sensitivity, and any rank instability before claiming altered scientific interpretation. SHAP and permutation scores do not imply clinical causality.

Only aggregate JSON is written; still requires disclosure approval. No row-level identifiers, predictions, SHAP matrices or feature arrays are exported by the analysis program.

## C. Publication and review gates

- Qiran's comments were made on a **v0.2 manuscript**, not on the latest v1.6.2. Retain her file unchanged as the original review record. Merge only unresolved editorial suggestions into the latest accessible Word file, preserving all Zotero fields.
- Before any new manuscript claim: confirm the tool run, default DQD thresholds, all failures, reproducibility data/version provenance, safe aggregate output, matched-population comparisons and sensitivity results.
- **If new QC and SHAP findings are incomplete:** Word may mention the planned extensions only in a separate analysis plan, **not** in the manuscript Abstract/Results/Conclusions.
- A new main figure is justified only if the result demonstrates a robust and distinct scientific claim. Otherwise use a supplementary QC table and supplementary attribution figure.
- This repo does not contain `.runrelay/project.yaml`; do not fabricate a task or dispatch a RunRelay job without a project-specific manifest and machine binding.
