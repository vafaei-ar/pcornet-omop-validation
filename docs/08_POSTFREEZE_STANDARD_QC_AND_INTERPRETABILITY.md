# Post-freeze standard OMOP QC and interpretability extension (proposed)

**State: database backup and restricted QC account provisioning completed locally on 2026-10-30; DQD, Achilles, and interpretability analyses NOT EXECUTED.** This extension is separate from the original frozen Stage A–E study and does not revise the approved primary estimates, database, phenotype, or outcome. All clinical row-level data remain within the authorized institutional environment. The local backup is 23.03 GiB compressed; SQL Server backup history, RESTORE VERIFYONLY WITH CHECKSUM, and frozen reference counts passed. An off-machine protected copy and isolated test restore are still pending.

- Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.
- Reference publication branch SHA: `1c271c37a9d1a279f075a8edee439fe0cce7706f`.
- Definition: `study_definitions/postfreeze_standard_qc_interpretability_v1.json`.
- R runner: `scripts/run_postfreeze_omop_qc.R`.
- Python interpretability engine: `src/pcornet_omop_validation/study/postfreeze_model_interpretability.py`.
- Synthetic model tests: `tests/test_postfreeze_interpretability.py`.

## A. OHDSI DQD and Achilles

The R packages support OMOP **v5.4**. The frozen target is OMOP v5.4.2, so use `cdmVersion = "5.4"`. See upstream official documentation: <https://ohdsi.github.io/DataQualityDashboard/reference/executeDqChecks.html>, <https://ohdsi.github.io/Achilles/reference/achilles.html>.

**Read-only JDBC and frozen-target preflight (run BEFORE both tools):**

```bash
# From repository root, with the same local R library used for installations:
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export R_LIBS_USER="$PWD/results/ohdsi_r_lib"
python scripts/preflight_postfreeze_omop_qc.py \
  --config config/etl_A.yaml \
  --driver-dir results/ohdsi_jdbc
```

**When JDBC reports `Login failed for user 'SA'`, first run the ODBC-only comparison:**

```bash
python scripts/preflight_postfreeze_omop_qc.py --config config/etl_A.yaml --diagnose-odbc
```

This uses the existing ETL SQLAlchemy/pyodbc connection and only issues `SELECT DB_NAME()`. The output is a sanitized success or broad error category and never includes raw connection strings, credentials, or server addresses. If both ODBC and JDBC reject the login, check the local password environment variable and SQL Server authentication configuration. If ODBC succeeds and JDBC fails, investigate driver/protocol, server instance, login database and JDBC settings. Do not change the SQL login password or enable elevated permissions just to satisfy the preflight.

The Python wrapper loads only the existing protected ETL YAML and its named password environment variable. It passes connection details privately in process environment to the read-only R preflight, without printing, persisting or requesting credentials in source files. The preflight **only runs SELECT queries** to verify the configured SQL database, frozen target counts (person 27,089, visit_occurrence 1,510,957, condition_occurrence 7,315,572), and selected login write privileges. A count mismatch **blocks QC**. If the existing ETL account is `sa`/sysadmin or has write rights to the frozen tables, a successful connection is **not** evidence that the QC privilege gate passed. Provision a dedicated least-privilege principal before running the full DQD/Achilles tasks. The query is intentionally small and does not test broad analytical workloads.

**Protected full backup prerequisite (completed locally):**

The prior ad hoc SQLAlchemy/pyodbc BACKUP attempt was canceled because it
closed the cursor before exhausting SQL Server's progress/status results.
The SQL Server backup directory was empty after this failure. Do not infer
backup success from a Python print message. The corrected code explicitly
drains `cursor.nextset()` until completion, checks `msdb` for a completed
`COPY_ONLY` backup with checksums and nonzero compressed media size, then
runs `RESTORE VERIFYONLY ... WITH CHECKSUM, STOP_ON_ERROR` using the same
safe cursor handling. This is based on the documented pyodbc issue:
<https://github.com/mkleehammer/pyodbc/issues/471>.

Only use an institution-authorized encrypted storage location. The backup
directory remains owned by the SQL Server service and outside the repository.
In particular, directory mode `0700` owned by `mssql` is expected. The
regular operator may receive `PermissionError` when trying to stat this
directory: the backup script now tolerates this and leaves SQL Server to
validate its own filesystem access. Before `--execute`, independently
verify `sudo -u mssql test -w /var/opt/mssql/backup`. Do not loosen permissions
and do not run the Python script with `sudo`.
A compressed backup can still be large. Verify ample free disk space and
no competing long-running analyses. An operator must explicitly request
execution; default invocation is a preview with no SQL connection:

```bash
python scripts/backup_frozen_omop.py --config config/etl_A.yaml
# Once server backup directory/permissions and protected storage are checked:
python scripts/backup_frozen_omop.py --config config/etl_A.yaml --execute
sudo ls -lh /var/opt/mssql/backup/
```

Do not interrupt a live BACKUP/VERIFYONLY; keep the client connected until
it reports `BACKUP AND VERIFICATION: PASS`. The verified backup is not a
substitute for an isolated test restore and an off-machine protected copy.
Do not upload `.bak` files, backup transcripts with secrets or row-level
clinical data to Git, chats, public storage or RunRelay artifacts.

**Restricted QC login provisioning (completed locally):**

Use a **separate QC output database**, not writable schemas inside the frozen CDM database. The default provisioner only prints a plan, with no SQL connection or writes:

```bash
python scripts/provision_postfreeze_qc_access.py --config config/etl_A.yaml
```

After confirming an administrator-authorized maintenance window, verified backup, and the plan, the operator can explicitly choose:

```bash
python scripts/provision_postfreeze_qc_access.py --config config/etl_A.yaml --apply
```

`--apply` is an **administrative security change**, not an ordinary analysis. It requires exact interactive confirmation and a newly chosen password entered without echo. It checks the existing frozen target count anchors before DDL, refuses pre-existing login/database names, creates the restricted `ohdsi_postfreeze_qc` login, creates `<frozen_database>_postfreeze_qc` as a separate output database with `qc_results` and `qc_scratch`, and grants read-only CDM schema access (explicit DML/ALTER denial) plus QC-output database scoped table-creation permissions. Only a new user and its SQL permission metadata are added to the frozen database. **No frozen CDM table is modified.** Provisioning has not been exercised against the actual instance; an administrator must review it before the first `--apply`. If execution partially fails, stop and inspect manually. Do not repeat `--apply` or delete existing resources automatically. Store the newly selected QC password only in an approved secret manager.

For subsequent **read-only restricted-login verification**:

```bash
read -r -s -p "QC account password: " OHDSI_QC_PASSWORD
printf '\n'
export OHDSI_QC_PASSWORD
python scripts/preflight_postfreeze_omop_qc.py \
  --config config/etl_A.yaml \
  --driver-dir results/ohdsi_jdbc \
  --qc-login
```

The restricted-login JDBC preflight completed successfully on 2026-10-30. Database identity and person/visit_occurrence/condition_occurrence reference counts passed; sysadmin, db_owner and selected CDM write permissions were NO. The follow-on read-only full permissions audit also PASSED on 2026-10-30: 75 frozen tables readable without write privileges, reference row counts unchanged, restricted permissions on separate QC results and scratch schemas verified, with zero pre-existing QC output tables. Never use `SA` for DQD or Achilles. This first preflight is necessary but not a complete grant audit.

**Next read-only permissions audit (pending):**

```bash
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export R_LIBS_USER="$PWD/results/ohdsi_r_lib"

# OHDSI_QC_PASSWORD must already be set securely in this shell.
python scripts/preflight_postfreeze_omop_qc.py \
  --config config/etl_A.yaml \
  --driver-dir results/ohdsi_jdbc \
  --qc-login \
  --audit-qc-access
```

This SELECT-only second audit reconnects as the restricted login to the frozen
CDM and then to the independent QC output database. It verifies the login
identity, server/database role restrictions, denied write permissions for
**all visible frozen tables**, the three frozen count anchors, database-level
CREATE TABLE in the QC output database, and schema-scoped CRUD/ALTER
permissions only on `qc_results` and `qc_scratch` (not `dbo`). It reports
the number of existing output tables and warns if any are present; review
those before Achilles with `createTable=TRUE`. This audit does not create
test tables or execute DQD/Achilles. `QC ACCESS AUDIT: PASS` is the required
completion signal. None of these permissions are inferred merely from a
successful JDBC connection.

**Controlled local DQD launch (not yet executed):**

The DQD runner uses OHDSI v2.9.1, `cdmVersion="5.4"`, all three
check levels, and unmodified DQD default thresholds. Its
`writeToTable=FALSE` setting saves the output JSON and logs locally rather
than inserting DQD results into SQL Server. The launcher pins the installed
package version, derives all database/schema settings from
`config/etl_A.yaml`, requires the dedicated `OHDSI_QC_PASSWORD`, and excludes
the ETL administrator password from R's environment. The launcher first
reruns the complete SELECT-only 75-table permission audit and then invokes the
R runner, which independently verifies target identity, selected write
permissions and frozen count anchors. The default invocation is a no-database
preview:

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export R_LIBS_USER="$PWD/results/ohdsi_r_lib"

# Preview: no SQL queries and no execution
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool dqd

# Explicitly execute, only with local institutional approval and
# OHDSI_QC_PASSWORD set securely in the current shell:
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool dqd --execute
```

Output files are under ignored `results/postfreeze_standard_qc/dqd/` with a
restricted file-creation mask. Existing output files block automatic reruns
to prevent accidental overwrite. A successful execution will produce a
private execution manifest. The raw DQD JSON may contain sensitive small
counts and SQL error details. Review and aggregate it locally before sharing
any findings. Do not run Achilles until the DQD execution and output have
been reviewed.

**Preparation by the local database/R administrator**:

1. Confirm that the configured server/database holds the exact **frozen publication target**, not another OMOP build. Record source snapshot and database identity privately.
2. Install and review R (>=4), Java, a local SQL Server JDBC driver and OHDSI `DatabaseConnector`, `DataQualityDashboard`, `Achilles`, `jsonlite`. Use institution-approved package versions. Existing R 4.1.2 meets published minimum versions, although OHDSI HADES recommends R 4.4.1 for maximum compatibility. Avoid replacing the system R without testing an isolated local library first.
3. Provision a separate **QC output database** containing dedicated **results** and **scratch** schemas writable by the QC login. Neither schema may be in the frozen target database. DQD runs with `writeToTable=FALSE`; Achilles writes only to the designated QC output database. Avoid adding QC results tables to the frozen publication database.
4. Confirm expected duration/resource impact with database administration. DQD can run thousands of checks. Achilles can execute substantial queries. Do not execute during ETL or competing heavy jobs.
5. Set environment variables locally (never commit values):

    `OHDSI_DB_SERVER` = SQL Server **hostname only**, e.g. `localhost` (not `hostname/database`; the latter format applies to PostgreSQL in DatabaseConnector)  
    `OHDSI_DB_USER` and `OHDSI_DB_PASSWORD` = restricted QC login and corresponding secret, never `SA`  
    `OHDSI_DB_NAME` = frozen database name from protected ETL configuration  
    `OHDSI_TLS_TRUST_SERVER_CERTIFICATE` = `true` or `false`, matching approved local TLS trust settings  
    `OHDSI_JDBC_DRIVER_PATH` = local folder containing Microsoft SQL Server JDBC `.jar` files (may be populated with `DatabaseConnector::downloadJdbcDrivers('sql server', pathToDriver=...)`)  
    `OHDSI_CDM_SCHEMA` = `database.schema` containing frozen OMOP  
    `OHDSI_RESULTS_SCHEMA` = `qc_output_database.qc_results` for QC results  
    `OHDSI_SCRATCH_SCHEMA` = `qc_output_database.qc_scratch` for scratch work  
    `OHDSI_QC_EXPECTED_PACKAGE_VERSION` = verified installed version for whichever tool runs  
    `OHDSI_QC_CONFIRM` = `RUN_POSTFREEZE_QC_ON_FROZEN_TARGET`

Run separately **only after** the restricted-login preflight, full schema privilege review, and explicit operator authorization. The runner now independently blocks privileged logins, mismatched frozen counts and output schemas inside the CDM database:

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
