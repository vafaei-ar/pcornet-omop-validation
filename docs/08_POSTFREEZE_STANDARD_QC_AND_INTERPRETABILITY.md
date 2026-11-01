# Post-freeze standard OMOP QC and interpretability extension (partially executed)

For the concise evidence closeout and manuscript readiness decisions, see
[`09_QC_CLOSEOUT_AND_MANUSCRIPT_FINALIZATION.md`](09_QC_CLOSEOUT_AND_MANUSCRIPT_FINALIZATION.md).
This document retains the execution history, original protocol, code paths,
restricted local procedures and still-unexecuted interpretability plan.


**State: local backup, QC account provisioning, and full permissions audit passed on 2026-10-30. DQD remains blocked because frozen CDM_SOURCE has zero rows, confirmed by read-only inspection. No DQD check results. Achilles 1.7.2 COMPLETED locally in 3.63 minutes, with 194 distinct analysis IDs marked complete in the transcript. The read-only persisted-results audit PASSED: 294 definition rows, 840,097 regular result rows, 44,802 distribution result rows, 334 distinct stored analysis IDs, zero permanent scratch tables, unchanged frozen publication counts, and CDM_SOURCE still empty. The completed read-only analysis-ID reconciliation PASSED: all 194 unmatched IDs are documented Achilles performance records (analysis_id + 2,000,000), none are unexplained, and 140 of 194 default analyses have data-bearing result rows. The remaining 54 default analyses lack stored data-bearing result rows, which is not automatically an execution or quality failure. The read-only category inventory PASSED: missing results concentrate in Visit Detail (11), Payer Plan Period (9), Condition Era (8), Drug Era (8), Care Site/Location/Provider (12 total), Note (2), Visit Occurrence (2), Observation (1), and Observation Period (1). Source-table presence triage PASSED: eight OMOP tables are empty (visit_detail, payer_plan_period, condition_era, drug_era, care_site, location, provider, note); visit_occurrence, observation and observation_period contain rows. Frozen-source ETL scope analysis identified a discrepancy between the original design and the 11-target freeze acceptance gate. Targeted Achilles review of IDs 207, 209, 210 and 814 completed against official v1.7.2 SQL. A protected local four-ID exception diagnostic executed, and its private output passed file structure/permissions validation. On 2026-10-31 the restricted live frozen-target reconciliation for ID 210 PASSED, with no discrepancy against the protected prior result and no new cohort materialization; its exact value remains private pending disclosure review. The remaining 207/209/814 exception classifications have not been independently interpreted, and the limitations of empty domains persist. Tests: 193 passed, 1 skipped. No DQD check results. Interpretability analyses NOT EXECUTED.** This extension is separate from the original frozen Stage A–E study and does not revise the approved primary estimates, database, phenotype, or outcome. All clinical row-level data remain within the authorized institutional environment. The local backup is 23.03 GiB compressed; SQL Server backup history, RESTORE VERIFYONLY WITH CHECKSUM, and frozen reference counts passed. An off-machine protected copy and isolated test restore are still pending.

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

`--apply` is an **administrative security change**, not an ordinary analysis. It requires exact interactive confirmation and a newly chosen password entered without echo. It checks the existing frozen target count anchors before DDL, refuses pre-existing login/database names, creates the restricted `ohdsi_postfreeze_qc` login, creates `<frozen_database>_postfreeze_qc` as a separate output database with `qc_results` and `qc_scratch`, and grants read-only CDM schema access (explicit DML/ALTER denial) plus QC-output database scoped table-creation permissions. Only a new user and its SQL permission metadata are added to the frozen database. **No frozen CDM table is modified.** Provisioning was executed once successfully on the local instance on 2026-10-30, followed by successful JDBC and full permissions audits; do not run provisioning again. If execution partially fails, stop and inspect manually. Do not repeat `--apply` or delete existing resources automatically. Store the newly selected QC password only in an approved secret manager.

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

**Read-only permissions audit (completed; retained for reproducibility):**

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

**DQD first-attempt diagnosis and fix (2026-10-30):**

The first `--tool dqd --execute` launch repeated the 75-table SELECT-only
permissions audit successfully. The R runner then passed the sampled
privilege gate but failed **before** calling `executeDqChecks` while reading
the first frozen count from `DatabaseConnector::querySql`.
The original snapshot code assumed the column alias is always uppercase
`N` and used `$N[[1]]`. On this JDBC path the returned object did not
contain that exact column name, so the count had length zero and
`vapply` rejected it. This is not a DQD data-quality failure.

The runner now reads the single count column by position, independently
of SQL alias case/name, and validates one row, one column, finite nonnegative
integer count. The same parser is used for the pre- and post-run snapshots.
Synthetic Python/R tests include lowercase, uppercase and arbitrary aliases,
empty results, NA and malformed counts. The original three publication
anchors still block the run on disagreement.

Before the next attempt, run `git pull --ff-only`, `pytest -q`, and:
```bash
Rscript -e 'invisible(parse(file="scripts/run_postfreeze_omop_qc.R")); source("scripts/postfreeze_qc_count_helpers.R"); stopifnot(identical(read_postfreeze_count_result(data.frame(n=27089),"person"),27089)); cat("Snapshot parser: PASS\\n")'
find results/postfreeze_standard_qc/dqd -mindepth 1 -maxdepth 2 -printf '%P\\n'
```
The first R invocation may have created an empty output directory. If
the `find` command lists files or subdirectories, preserve and inspect
them first; the guarded launcher intentionally refuses overwriting
existing output. Do not delete generated results to force a retry.

**DQD second-attempt metadata blocker (2026-10-30):**

After correcting the snapshot parser (all local tests passed, 81 passed,
1 skipped), DQD passed the fresh 75-table read-only permissions audit,
privilege gate and frozen count anchors. It then refused initialization
with `Please populate the cdm_source table before executing data quality checks.`
No DQD check results were generated. OHDSI explicitly requires at least
one properly populated `CDM_SOURCE` record for DQD metadata:
<https://ohdsi.github.io/DataQualityDashboard/index.html>.
The `cdmSourceName` argument does **not** replace this requirement.

The frozen `CDM_SOURCE` table must **not** be populated in place to make
DQD run, and QC SELECT-only privileges must not be loosened. A missing
or incomplete CDM_SOURCE is itself a provenance/data quality finding.
The read-only metadata inspection completed locally: **CDM_SOURCE contains
zero rows**, all 11 expected columns are present, and a nonempty vocabulary
version metadata record is available. The inspection confirmed no SQL
writes. The diagnostic can be reproduced as follows:

```bash
# Reuse the securely set OHDSI_QC_PASSWORD and existing R/JDBC setup.
python scripts/preflight_postfreeze_omop_qc.py \
  --config config/etl_A.yaml \
  --driver-dir results/ohdsi_jdbc \
  --qc-login \
  --inspect-cdm-source

# Inspect only names of any locally generated, private partial DQD outputs.
find results/postfreeze_standard_qc/dqd -mindepth 1 -maxdepth 2 -printf '%P\\n'
```

Only after confirming the exact missing metadata should an analyst consider a
separately restored **analysis copy** of the frozen database with a documented,
authentically sourced CDM_SOURCE record. Such a copy is not the original
publication target. It must remain institution-local, be permission-isolated,
and have all source clinical tables independently checked for concordance.
No missing dates, version concept identifiers, vocabulary versions, or
institutional source metadata should be invented. A metadata-overlay
workaround or bypassing the DQD prerequisite would change what is measured
and would need explicit scientific justification. Until a valid isolated
target exists, DQD stays blocked. **Achilles is a separate descriptive
characterization tool**, not proof that DQD passed. Unlike DQD, Achilles
supports explicitly supplying `sourceName` and `cdmVersion` instead of
deriving them from CDM_SOURCE. Our runner supplies both. It can therefore
be attempted independently using the existing separate QC output database,
subject to the normal privileges and resource gates. Its success is not
guaranteed by the DQD diagnostic. This exploratory run must prominently
report that the frozen CDM_SOURCE was empty.

**Controlled local DQD launch (blocked pending metadata diagnosis):**

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
any findings. Do not rerun DQD against the frozen database while CDM_SOURCE is empty.
Achilles can be attempted independently, after reviewing the zero pre-existing
QC output tables and passing the full permissions audit. The R runner now
includes CDM_SOURCE in its frozen pre/post count snapshot; it blocks DQD
immediately, but warns on Achilles and records the zero metadata row count
and unresolved deficiency in the local execution manifest. To preview or
explicitly launch Achilles after tests pass:

```bash
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool achilles
# Explicit operator-authorized execution, not part of a DQD pass:
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool achilles --execute
```

Achilles writes only to the separate QC database (results/scratch) with
`smallCellCount=10`, not to the frozen CDM. The first local Achilles execution
on 2026-10-30 completed with an R runner success message. The full terminal
transcript contains 194 distinct Achilles analysis IDs, with all 194 marked
`[COMPLETE]` and no `[FAILED]` or `[ERROR]` lines. Total Achilles runtime:
3.631022 minutes. Terminal messages reported merged results under
`OMOP_VALIDATED_A_postfreeze_qc.qc_results` and temporary scratch cleanup.
The post-run frozen table snapshot also completed and the runner generated
its local execution manifest. The missing `CDM_SOURCE` record was explicitly
reported and is NOT repaired by Achilles. These logs alone do not verify
persisted table contents or constitute a DQD pass.

**Achilles post-run audit (completed and passed):**

The first audit attempt on 2026-10-30 passed the ignored local Achilles
execution manifest, but incorrectly reported `achilles_analysis` missing
before querying any persisted Achilles result counts. This was an **audit
bug**, not an Achilles execution error: the Python table-name comparison
was case-sensitive, while Achilles v1.7.2 explicitly creates
`ACHILLES_ANALYSIS` uppercase in the SQL Server DDL and inserts into the
same uppercase table. Upstream implementation:
<https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/achilles_analysis_ddl.sql>
and
<https://github.com/OHDSI/Achilles/blob/v1.7.2/R/Achilles.R>.
The corrected Python auditor discovers the *actual* table/schema names
using SQL Server metadata, compares them case-insensitively, preserves actual
case in properly quoted queries, and fails if required tables are genuinely
absent, ambiguous or empty. It prints table names only if a table is still
missing. No new Achilles execution, table replacement, DDL or DML is needed.

Run the following after pulling this update and verifying tests:

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q
# Reuse the dedicated OHDSI_QC_PASSWORD; never use the SA login.
python scripts/inspect_postfreeze_achilles.py --config config/etl_A.yaml
```

This `SELECT`-only Python auditor uses the dedicated QC login, not the ETL
administrator credential. It checks the local completion manifest and the
reported unresolved CDM_SOURCE deficiency, confirms saved core Achilles result
tables and table-row counts (metadata only, NOT patient counts), counts the
distinct analysis IDs represented in result rows, checks any remaining
permanent scratch tables, and re-verifies all three frozen publication counts.
It does NOT display concept-level results, stratum labels, individual patient
values, raw Achilles outputs, or suppressed cells. The necessary final signal
is `ACHILLES READ-ONLY POST-RUN AUDIT: PASS`. The corrected read-only post-run audit PASSED locally on 2026-10-30
following 103 passed Python tests and 1 skipped test. The saved counts are:
`achilles_analysis` 294 rows, `achilles_results` 840,097 rows,
`achilles_results_dist` 44,802 rows, and 334 distinct analysis IDs in
the union of saved result tables. No permanent scratch tables remained, and
the frozen person, visit_occurrence and condition_occurrence counts still
matched their publication references. These values are Achilles **table
row counts and analysis identifiers**, not patient counts or quality
pass/fail measures.

**Achilles analysis-ID reconciliation (completed and passed):**

The first read-only reconciliation completed on 2026-10-30, with 115 local
tests passing and 1 skipped. It found 259 distinct regular-result IDs,
77 distinct distribution-result IDs, and 2 IDs in both sets, for 334 total.
All 294 definition rows have distinct non-NULL IDs, with 194 stored result IDs
not directly matching a definition, and 154 definitions not represented by
a data-bearing result ID. No NULL analysis IDs were found.

The first 20 unmatched IDs start with `2000000, 2000001, ...`. In
Achilles v1.7.2, the official SQL Server performance export explicitly joins
results using `achilles_analysis.analysis_id + 2000000 =
achilles_results.analysis_id`:
<https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/export/performance/sqlAchillesPerformance.sql>.
The versioned analysis-details CSV contains 294 definitions, 194 default and
100 non-default:
<https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/csv/achilles/achilles_analysis_details.csv>.
The **second full-set reconciliation PASSED** locally on 2026-10-30 with
124 tests passed and one skipped: all 194 initially unmatched IDs map exactly
to a defined analysis ID plus 2,000,000, zero truly unexplained IDs exist,
194 performance IDs belong to default analyses, and zero performance IDs
belong to non-default analyses. Of the 194 default analyses, 140 have
data-bearing result rows and 54 have none. All 100 non-default analyses
appropriately lack result rows under `defaultAnalysesOnly=TRUE`. This
explains all 154 definitions without data-bearing rows. **No result row
does not mean a quality failure**. Achilles' output suppression, absent source
records or SQL returning no rows remain alternative possibilities until
the relevant analyses and source-domain coverage are inspected.

**Reproduce the passed SELECT-only namespace reconciliation:**

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q
# OHDSI_QC_PASSWORD is already configured securely in the current shell.
python scripts/reconcile_postfreeze_achilles_analysis_ids.py \
  --config config/etl_A.yaml
```

The corrected auditor checks whether each initially unmatched ID is exactly
2,000,000 plus a defined ID, separates genuinely unexplained IDs, and counts
performance and data-bearing IDs for default/non-default definitions. It
requires 194 default and 100 non-default definitions, zero unknown IDs, no
unexpected non-default execution with `defaultAnalysesOnly=TRUE`, no NULL or
duplicate metadata IDs, and consistent SQL set arithmetic before reporting
`RECONCILIATION: PASS`. Only up to 20 genuinely unexplained OHDSI analysis
IDs appear if a mismatch remains. No clinical values, strata, person IDs,
or result `count_value` fields are read. The original SQL Server database
and QC tables remain unchanged.

**Achilles category-level inventory of missing data-bearing results (completed and passed):**

Use the verified results to identify which of the 54 default analyses have
no surviving rows. Only definition IDs, OHDSI-provided category labels,
default flags, and the presence/absence of analysis IDs in results are read.
No count values, strata, patient records, clinical concept IDs, or raw
distribution values are accessed. The script validates 294 definitions,
194 default, 100 non-default, 194 performance IDs, 140 default IDs with data,
54 default IDs without data, and zero non-default IDs in stored results.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q
# Requires OHDSI_QC_PASSWORD already securely set in the current shell.
python scripts/summarize_postfreeze_achilles_coverage.py \
  --config config/etl_A.yaml
```

The read-only category inventory completed locally on 2026-10-30 with
134 tests passed and 1 skipped. The 54 default analyses without stored
data-bearing rows consist of Visit Detail 11, Payer Plan Period 9,
Condition Era 8, Drug Era 8, Care Site 4, Location 4, Provider 4,
Note 2, Visit Occurrence 2, Observation 1 and Observation Period 1.
All 12 Condition Occurrence, 13 Drug Exposure, 23 Measurement,
12 Death, 10 Person and 10 Procedure Occurrence default analyses had
stored results. (All counts here describe **Achilles analysis definitions**,
not patient numbers.) The optional `--show-missing-analysis-ids` flag
reveals only OHDSI analysis IDs.

The concentration in the era categories merits attention: the event-based
Condition Occurrence and Drug Exposure categories show complete analysis-ID
coverage while their corresponding derived era categories show none. This
might occur because era tables are empty, the approved ETL did not construct
eras, or analyses returned no persistent rows. It cannot be identified as
an ETL defect until checking source-table presence and ETL design. Empty
or unpopulated provider/insurance-related tables might similarly explain
some gaps. The single Visit Detail analysis with a result does not establish
that `visit_detail` has records. A zero-count summary can persist on an
empty table.

**Source-table presence triage (completed, read-only):**

The local run on 2026-10-30 passed with 144 tests passed, 1 skipped.
The independent QC and frozen anchor checks remained successful.
The diagnostic showed **zero rows** in eight OMOP tables:
`visit_detail`, `payer_plan_period`, `condition_era`, `drug_era`,
`care_site`, `location`, `provider`, and `note`.
`visit_occurrence`, `observation`, `observation_period`,
`condition_occurrence`, `drug_exposure` and `person` each
contained one or more rows. The diagnostic used SQL `EXISTS` and printed
YES/NO only, not source counts or clinical values. Importantly, one
Visit Detail Achilles output exists despite the empty CDM table, likely
because a high-level count analysis can emit a result for an empty table.
No data-bearing row for an Achilles analysis does **not** establish that
the source domain is empty, and a successful Achilles run does not certify
ETL completeness.

**Frozen ETL implementation-versus-design review:**

We inspected the immutable ETL code at
`887e6f4d60a6b185e58b3c9fe8887472b49777e3` and its archived
design/freeze documentation. This is a **scope audit**, not a clinical
data comparison:

| Tables / feature | Frozen implementation evidence | Interpretation and risk |
| --- | --- | --- |
| `condition_era`, `drug_era` | Source event tables populated; neither derived era appears in the 11-table Phase 12 target requirements or clean-reset target list; no era materialization stage among the frozen phase modules | Confirmed absent in this build. Analyses requiring native OMOP era tables cannot be assumed reproducible. Whether this was explicitly approved for the study requires documenting scope. |
| `visit_detail`, `payer_plan_period`, `note` | Empty in SQL; omitted from Phase 12 accepted target list; no dedicated phase materializes these tables | Unimplemented/unsupported in the publication build. Do not label as source-record loss without establishing source coverage and original ETL obligations. |
| `care_site`, `location` | Empty in SQL; omitted from Phase 12 target list. The original `docs/etl_redesign.md` proposed ENCOUNTER-to-`care_site`/`location` and LDS_ADDRESS_HISTORY-to-`location`, and source preflight requires `lds_address_history.parquet` | **Design versus frozen-implementation gap.** Source file requirement is not proof it contains eligible rows. Missing dimension materialization must be disclosed and reconciled against approved scope, not silently called intentional. |
| `provider` | Empty in SQL; original PCORnet `provider.parquet` is optional in source preflight, and `docs/04_REPRODUCIBILITY_AND_RERUN.md` explicitly records its absence from this dataset | Input limitation. Do not attribute empty OMOP provider to a dropped populated PCORnet provider table. Other provider fields/semantics remain unverified. |
| `cdm_source` | Empty in SQL, while the original redesign explicitly proposed recording ETL and vocabulary provenance there; absent from Phase 12 required targets | **Confirmed provenance/ETL metadata gap.** DQD cannot run on the frozen publication target. |
| `visit_occurrence`, `observation`, `observation_period` | Tables have records but 2, 1, and 1 default Achilles analyses, respectively, lack data-bearing results | Requires inspection of those particular OHDSI analysis IDs. Could reflect zero-row SQL, suppression, or missing qualifying strata; do not infer missing source data. |

More specifically, frozen `etl/visit_occurrence.py` inserts `NULL`
for `provider_id` and `care_site_id`, while `etl/person.py` does
not populate `location_id`. This confirms that at least those
cross-domain links are not materialized by the frozen core transforms.
The frozen Phase 12 validator requires exactly these 11 core OMOP
targets: `person`, `observation_period`, `visit_occurrence`,
`condition_occurrence`, `procedure_occurrence`, `measurement`,
`observation`, `drug_exposure`, `device_exposure`,
`specimen` and `death`. Its pass does **not** claim full-table OMOP
population. The earlier redesign was a plan, not evidence that every
planned domain reached the frozen release.

**Targeted four-ID Achilles analysis review (2026-10-30, completed from
metadata plus official OHDSI Achilles v1.7.2 SQL):**

The local coverage inventory printed `Visit Occurrence: 207, 209`,
`Observation Period: 210`, and `Observation: 814`. Analysis 210 is
categorized as Observation Period in the local Achilles metadata, but
its SQL tests `visit_occurrence` against `observation_period`.

| Analysis ID | Official SQL predicate / count target | Interpretation in the frozen ETL |
| --- | --- | --- |
| [207](https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/207.sql) | `COUNT_BIG(vo.person_id)` after a left join from visits to persons, where no person matches | A nonzero count would flag invalid person references. It does not check null `person_id` rows because of `COUNT_BIG(column)`; the CDM makes `person_id` required. |
| [209](https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/209.sql) | `COUNT_BIG(vo.person_id)` where the visit `care_site_id` is non-NULL but fails to match `care_site` | The frozen visit ETL deliberately inserts NULL for `care_site_id` and the care_site table is empty; a zero result is expected under those rules. Missing care-site information remains a separate domain-coverage limitation. |
| [210](https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/210.sql) | `COUNT_BIG(*)` for visits without any observation period covering the **visit start date** for the same person | A positive count indicates temporal coverage discordance (or no qualifying observation period), not necessarily an incorrectly constructed visit. Relevant to study eligibility and follow-up semantics. |
| [814](https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/814.sql) | `COUNT_BIG(o.person_id)` where `value_as_number`, `value_as_string` and `value_as_concept_id` are all NULL | This is an Achilles no-value screen, but the OMOP v5.4 Observation value columns are optional. A bare clinical assertion may legitimately lack an explicit value; inspect semantics before treating it as information loss. |

These four SQL files use ungrouped aggregate count queries and therefore
produce a count row even for zero qualifying records. The official
[merge SQL](https://github.com/OHDSI/Achilles/blob/v1.7.2/inst/sql/sql_server/analyses/merge_achilles_tables.sql)
retains only `count_value > @smallCellCount`; this run used
`smallCellCount=10`. Consequently, **given the successful execution,
merge and persisted-result audits and no later output changes**, each
absent data-bearing result implies a count in the range **0 to 10**.
The absent rows cannot certify that all four counts were zero or that
the underlying data passed an integrity check. These are **suppressed
exception-count outputs**, not failed Achilles execution or absent
source domains.

**Next interpretation gate, not executed:** If the manuscript needs a
definitive distinction between zero and suppressed low-frequency
exceptions, perform four narrowly scoped `SELECT`-only checks inside
the authorized institutional environment, preserving exact counts and
event-level evidence locally. Prioritize 210 because even a few visits
outside observation periods may affect eligibility if they overlap
the locked study cohort. Check the local result against the locked
cohort/eligibility logic before claiming any downstream impact. Review
207 for referential integrity, 209 for the known null-link policy, and
814 for concept-specific meaning. Do not paste small cell counts,
identifiers or clinical rows into chat or commit them to Git. This
local distinction is not needed to document that Achilles ran with
small-cell suppression. The frozen ETL, its published counts and
the original results remain unchanged.

**Executable, restricted, exact-count diagnostic (executed locally on
2026-10-31; results protected):**

The new `scripts/audit_postfreeze_achilles_exception_counts.py`
implements literal `SELECT COUNT_BIG` versions of the official OHDSI
v1.7.2 predicates for 207, 209, 210 and 814, without `INTO` scratch
tables. Before querying any exception counts it verifies the ignored local
Achilles manifest, all 194 default performance statuses and 54 missing
outputs, the four specific missing IDs, the restricted login/database
identity and the frozen anchor counts plus empty CDM_SOURCE.
Only the already-provisioned read-only QC login can connect.

The script blocks if an exception count exceeds the original
`smallCellCount=10` threshold. It never prints any of the four counts,
identifiers, strata or clinical values. It writes the four exact counts
and zero-versus-low-count classifications to a **new private local file**
with mode `0600` in a `0700` directory. The output is ignored by Git.
Existing files are never overwritten, and symlinked or less-restricted
private directories block the script. The only output to the terminal is
the status and private file path. These local small counts remain
confidential and require formal institutional disclosure review before
use elsewhere.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q

# Set the already provisioned restricted QC password securely in this
# terminal if it is not already present. Do NOT paste it into chat.
if [ -z "${OHDSI_QC_PASSWORD:-}" ]; then
  read -r -s -p 'QC password: ' OHDSI_QC_PASSWORD
  printf '\n'
  export OHDSI_QC_PASSWORD
fi

python scripts/audit_postfreeze_achilles_exception_counts.py \
  --config config/etl_A.yaml
```

**2026-10-31 correction for an existing local private file:** On the
operator's first reported rerun, 155 tests passed, 1 skipped, and the new
synthetic stdout test failed because `inspect()` inspected an *actual*
previously existing private output path. The subsequent production diagnostic
correctly blocked rather than overwriting that path. The test now redirects
all private paths to `tmp_path`; no clinical file is required for tests.

When the private output file already exists, do **not** remove it to
force a rerun. Instead, after pulling and passing tests, use the new
metadata-only mode:

```bash
python scripts/audit_postfreeze_achilles_exception_counts.py \
  --verify-existing
```

This does not use the database or require the QC password. It checks owner,
file and directory permissions, JSON structure, fixed Achilles/CDM/ETL
metadata, timestamp, the four IDs, permitted 0-10 range and internal
classification consistency without printing any count or category. A
`STRUCTURE/PERMISSIONS PASS` confirms only file integrity/shape, **not**
authenticity of the SQL execution, provenance of its contents, or cohort
impact. If the check fails, preserve the file and troubleshoot locally.
The protected file must never be attached or quoted in chat. This new
validation mode is not a substitute for independent SQL QA and does not
complete the analysis-210 cohort overlap review.

Successful local execution reports `FOUR-ANALYSIS READ-ONLY TRIAGE: PASS`
and stores the private JSON at
`results/postfreeze_standard_qc/private_achilles_exception_counts/four_analysis_exact_counts.json`.
Do **not** paste, email, upload, publish, or commit the JSON. A repeat
invocation deliberately refuses to overwrite it.

**Analysis 210 locked D0 cohort-overlap audit (completed locally via the
no-SQL shortcut on 2026-10-31):**

The local private four-analysis report may contain zero or 1-10 exception
visits for Achilles 210. Its `--verify-existing` run passed structural and
permission checks, but that alone does not establish authenticity or provide
the confidential exception count.

`scripts/audit_postfreeze_achilles_210_cohort_overlap.py` first verifies
the existing protected four-ID JSON. When analysis 210 has **zero**
exceptions, it writes a private zero-overlap conclusion without SQL access.
When the count is positive, it uses only the dedicated restricted QC login
and read-only `SELECT` CTEs to reconstruct the **locked Stage C/D D0**
source and lineage-faithful OMOP populations directly from the frozen
source/code/lineage tables. The query replicates original D0 first qualifying
primary stroke diagnosis, episode selection, date fallback, adult selection,
and transformed OMOP condition/visit xwalk rules, with the original anchors
(9,815 source and 6,001 lineage-faithful OMOP). The Achilles 210 exception
predicate is reproduced as a `NOT EXISTS` observation-period check on
visit start date. It is not a new phenotype or a generic stroke cohort.

The private report includes aggregate overlap at visit and patient levels
and whether an exception could be the selected OMOP index visit or an
acute-care follow-up visit in the original 30- or 90-day outcome window.
Such overlap establishes **potential relevance**, not changed eligibility,
events, risk estimates or model coefficients. A zero overlap cannot repair
the independently established domain/metadata deficiencies. Counts remain
private and suppressed on the terminal. The script blocks if either
frozen cohort anchor or Achilles 210 exception count fails to reproduce.
No patient IDs, visit IDs, individual dates, SQL scratch tables or writes
are exported. Existing private output is never overwritten.

Run only after `pytest -q` passes in the active local branch:

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q

# The restricted OHDSI_QC_PASSWORD must be set securely for a nonzero
# Achilles 210 exception count; if zero, no SQL connection is needed.
python scripts/audit_postfreeze_achilles_210_cohort_overlap.py \
  --config config/etl_A.yaml
```

**2026-10-31 status and independent count verification gate:**

The institutional run on branch `5a69cd0` passed **177 tests, 1 skipped**.
The prior protected Achilles JSON passed its structure/permission checks, and
the Achilles 210 overlap program completed with a private output file. Its
terminal output contained no frozen anchor preflight messages. This is
consistent with its **zero-exception shortcut**: in this code path the
program trusts the previously saved private analysis-210 count and does
not connect to SQL Server or materialize the D0 cohort. Therefore the
completion message is **not** evidence of an independently rechecked
zero-exception count or newly reproduced D0 anchors.

To close this specific provenance gap without overwriting any artifact,
`scripts/verify_postfreeze_achilles_210_live.py` now performs an
independent, SELECT-only live count verification. It validates both
protected files locally, checks the original Achilles run manifest and
saved four-ID coverage under the restricted QC principal, rechecks the
three frozen publication table anchors and empty CDM_SOURCE, and executes
the **official v1.7.2 analysis-210 count predicate** directly on the frozen
OMOP target. It requires equality with the private saved count and
suppression bound (0-10). Any discrepancy blocks with **no count disclosed**.
It creates no result files, SQL scratch tables or database writes. It does
not rerun Achilles or D0, and its PASS does not certify effects on cohort
eligibility, estimates or clinical interpretation.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q
# Reuse the existing OHDSI_QC_PASSWORD in this terminal, set securely.
python scripts/verify_postfreeze_achilles_210_live.py \
  --config config/etl_A.yaml
```

The expected terminal signal,
`ACHILLES 210 LIVE FROZEN COUNT RECONCILIATION: PASS`, was observed
locally on 2026-10-31. The operator reported **193 tests passed,
1 skipped**, frozen publication row-count anchors passed, and the
restricted SQL Server 210 count reconciled with the protected result.
No identifiers, row-level data or suppressed count values were printed.
The existing private overlap artifact was internally consistent with the
same prior evidence. **No new D0 cohort was materialized.** The analysis-210
result interpretation is now locally corroborated; no independent claim
about D0 estimates follows from this check alone. No more Achilles
runs or analysis-210 verification scripts are necessary for this audit
unless input data or QC outputs change.

**Interpretability closeout (non-disclosive):** The four stored
performance records were successful. The missing data-bearing outputs
were caused by the configured small-cell threshold or absent/empty
inputs, rather than by a failed Achilles run. The local ID-210
verification is complete. Do not interpret missing rows as DQD passes,
CDM domain completeness, or confirmation of all relational-integrity
rules. The 207/209/814 private classifications require restricted
clinical/mapping review only if they affect a claimed validation
property. The most consequential outstanding QC limitation is the
empty frozen `CDM_SOURCE`, which blocks DQD. The frozen build also
omits several planned or derived OMOP domains. Resolving DQD requires
authentic provenance metadata in a separately restored, controlled
analysis copy, not an edit to the frozen publication target. Neither
the population-selection results nor the published estimates have
been revised by this QC extension.

Output remains under ignored, protected `results/postfreeze_standard_qc/private_achilles_exception_counts/analysis_210_locked_d0_overlap.json`.
Do not upload or paste this file. Report only the command exit status.
The protected overlap output was created locally. In this execution, the
program took the no-SQL shortcut, and the later live frozen-target count
reconciliation passed. The D0 CTE was not executed; no D0 overlap or
outcome estimate was recalculated. If the relevant frozen data or QC
outputs change in the future, review the audit preconditions before any
new execution. Preserve both private files.

 If a status is
`SUPPRESSED_NONZERO`, investigation must remain within the approved local
environment; no specific patient or encounter is inferred from the
aggregate. Analysis 210 must be interpreted against the locked cohort
logic **before** making any claim about scientific impact. This
diagnostic by itself does not test locked-cohort overlap and should not
be described as doing so.



Separately, resolve the `cdm_source` provenance requirement only in
a documented, institution-authorized **analysis copy** with verified
original metadata. Do not populate the frozen publication table or
change the frozen study's source-to-target estimands. No Achilles rerun
is needed for this interpretation.

Do not rerun Achilles with `createTable=TRUE` while completed results exist,
because that could overwrite them. Preserve raw results privately pending
disclosure review and independent scientific interpretation.

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

**Input prerequisite, now satisfied in the institutional environment on 2026-11-01:** four institutional local Parquet extracts with the **exact same six locked predictors**, a stable common patient key `patid`, `index_date` and the locked 90-day binary `outcome`. Only one row per patient. The extracts MUST be independently derived using the already completed Stage C/D/E definitions (including the provenance-aware target-date fallback overlay). Do not fill missing values or manufacture fallback records just to satisfy expected row counts.

| Local private input | Expected 90-day eligible / events | Required relationship |
| --- | ---: | --- |
| `pcornet_full` | 6,508 / 1,798 | Full source D0 with 90-day observability |
| `pcornet_shared` | 3,822 / 1,132 | Exact subset of full PCORnet, matching patient/episode/feature values |
| `omop_frozen` | 3,822 / 1,132 | Same patients, index dates and 90-day labels as shared PCORnet |
| `omop_fallback` | 6,508 / 1,798 | Same patients, index dates and 90-day labels as full PCORnet, using source-lineage-preserving encounter-date overlay |

**Pre-extraction gate (added 2026-10-31; no SQL and no private files):**

Before any clinical extraction, install all required libraries in the
institutional virtual environment, then validate tests, prior locked
Stage E preflight/Stage C fallback artifacts, SQL read-hook signatures,
package availability and private output path:

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
python -m pip install -e '.[etl,analysis,interpretability,dev]'
pytest -q
python -m pcornet_omop_validation.study.postfreeze_interpretability_extract \
  --config config/etl_A.yaml \
  --output-dir results/private_inputs \
  --preflight-only
```

The preflight performs **no SQL queries, no clinical reads and no writes**.
The hardened exporter confines output to this checkout's ignored
`results/` tree, forbids symlink escapes and nonempty output folders,
requires `0700` private working directories, writes create-only
`0600` Parquet files and checks identity, outcome, index date, binary
features, finite numeric values and locked cohort/event anchors before
writing. Existing files must be preserved, never deleted automatically.
A passing synthetic suite and preflight do **not** prove that the
institutional extraction will succeed or that feature derivations agree
scientifically with the locked Stage E code. Review the first run
locally and retain all row-level outputs solely within the approved
institutional environment.

**Local installation and preflight attempt (2026-10-31, verified terminal):**
`pip install -e '.[etl,analysis,interpretability,dev]'` succeeded,
including XGBoost 3.2.0 and SHAP 0.51.0. The repository tests returned
**201 passed in 2.15s, zero failed**. The subsequent `--preflight-only`
invocation stopped with `RuntimeError: Locked Stage E preflight file is
missing` while checking the required local
`stage_e_statistical_model_preflight.json`. No SQL query or
patient-level extraction was reached. Earlier Stage E execution records
show that its outcome-free preflight passed in September 2026; the
actual file's location or current availability must be checked locally.
**Do not create a placeholder JSON, ignore the guard, or launch the
private exporter before authentic evidence is restored.**

The next action is **read-only local artifact inventory**, not a
clinical rerun. Inspect the computed expected path and locate copies
under ignored `results/`. Compare any candidate's frozen ETL SHA,
status, study-definition hashes and no-outcome/no-model flags before
considering it suitable. Also check the required Stage C encounter-date
fallback result. If original evidence is genuinely absent, a controlled
outcome-free Stage E preflight may be repeated in the authorized
institutional environment and documented as *re-created metadata*,
not falsely dated as the original preflight. Do not change the locked
Stage E definitions or frozen target. The previous attempt to run
RF/XGBoost feature extraction did **not** occur.

**Stage E metadata recovery (2026-10-31 inventory):** The requested
read-only inventory confirmed that the expected
`results/publication_analysis/stage_e_statistical_model/stage_e_statistical_model_preflight.json`
is absent and that no file with this name exists anywhere under
`results/`. The authentic completed Stage C fallback report **is**
present at its expected location. The original historical Stage E
preflight cannot be copied or inferred from its completed Stage E models.

A guarded recovery script now executes the **unmodified original
outcome-free Stage E preflight checks**, but supplies the dedicated
`ohdsi_postfreeze_qc` login in memory instead of the ETL administrator.
It first verifies the frozen database identity and the unchanged
publication row-count anchors; requires the repository to be clean;
refuses existing output; checks the original frozen definition SHA
values and all required field checks; then saves a `0600` local JSON
containing a clear `postfreeze_recovery_provenance` field. It never
recreates historical timestamps or claims the new metadata is the
September preflight. It does not execute outcomes, models or ETL writes.
It creates no patient-level files.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q

# Requires the already-provisioned restricted OHDSI_QC_PASSWORD.
python scripts/recover_postfreeze_stage_e_preflight.py \
  --config config/etl_A.yaml

# Only after metadata recovery reports PASS:
python -m pcornet_omop_validation.study.postfreeze_interpretability_extract \
  --config config/etl_A.yaml \
  --output-dir results/private_inputs \
  --preflight-only
```

**Do not** rerun the original Stage E models, fabricate the missing
file, overwrite an existing preflight, or call `--confirm-private`
before both commands pass. No institutional recovery execution or
new test results are claimed at this stage.

**Verified preflight outcome and next protected extraction gate (local run
2026-11-01):** The institutional terminal reported `209 passed in 2.21s`.
The restricted outcome-free Stage E metadata recovery reported PASS, frozen
publication anchors remained unchanged, the recovered JSON was labeled as
regenerated, and the subsequent private RF/XGBoost `--preflight-only` reported
`PRIVATE RF XGBOOST PREFLIGHT PASS`. No row-level extraction or model fitting
was performed in that step.

Before the first row-level extraction, code was hardened again to require
`OHDSI_QC_PASSWORD` and route **both** the unchanged original Stage E feature
queries and Stage D encounter-date fallback feature queries through the
dedicated `ohdsi_postfreeze_qc` account. The original database-engine
bindings are restored even when an exception occurs. The script preflights
the locked artifacts again before any clinical query and verifies frozen
table counts via the restricted login. The command-line clinical export
suppresses SQL-provider tracebacks that might expose credentials or clinical
values. New synthetic tests cover restricted login, engine restoration,
required credentials, and sanitized terminal failure.

**These latest exporter code changes have not yet run in the institutional
environment.** Run the updated `pytest -q` first and only then authorize
the local extract under the existing, ignored, private output path.
It writes four row-level Parquet files and protected working outputs;
do not upload, paste, email, commit or publish the files, nor paste any
detailed SQL/provider traceback. The script refuses a nonempty output
directory. Preserve partial files if execution stops; review the reason
rather than deleting or retrying automatically.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q &&
python -m pcornet_omop_validation.study.postfreeze_interpretability_extract \
  --config config/etl_A.yaml \
  --output-dir results/private_inputs \
  --confirm-private
```

On success, the console should state
`private_four_population_feature_export_complete` with only aggregate
population sizes (no identifiers). Before modeling, verify all four
Parquet files' ownership and permissions locally and execute the model's
population/label/index checks. **No extraction or attribution result is
yet claimed.**

**2026-11-01 protected-export pytest regression and package fix:**
The next institutional test run completed **210 passed, 3 failed**.
All three failing tests attempted to import the top-level `scripts`
directory as a Python package. Pytest's import mode and the editable
`src/` package do not expose that directory in all environments.
The exporter itself had the same import error, so although pytest
prevented the clinical command, execution would also have failed.

The restricted frozen-target policy (local server, CDM v5.4.2,
`ohdsi_postfreeze_qc`, frozen identity, frozen publication row-count
anchors, and empty `CDM_SOURCE`) now lives in an **importable**
`pcornet_omop_validation.study.postfreeze_restricted_qc` module.
The exporter and the three tests use that package path. New synthetic
tests assert the restricted login, frozen anchors, rejection of
remote/wrong databases, read-only query forms, and failure on changed
CDM_SOURCE counts. The older completed Achilles inspection script
remains unchanged. No clinical rows were queried and no private
features exported in the failed run.

**The new corrections have not yet been tested on the institutional
host.** The next gate is to run `pytest -q` after `git pull --ff-only`
and authorize the original `--confirm-private` command only if all
tests pass. Do not delete, paste or upload protected files.

**2026-11-01 institutional extraction completion and model gate:** The
restricted QC login and frozen row-count anchors passed. The institutional
pytest suite returned **221 passed in 2.06s**. The unchanged Stage E
analysis was repeated in the private working directory with all locked
source and OMOP D0, 90-day cohort, event and fixed-index outcome anchors
reproduced. The independent encounter-date fallback Stage D query
reproduced the full source D0, 30/90-day eligible populations and event
counts, and exact same-patient outcome labels. The guarded exporter
reported `private_four_population_feature_export_complete` and created
four ignored, local, 0600 Parquet files under `results/private_inputs/`:
`pcornet_full` (6,508), `pcornet_shared` (3,822),
`omop_frozen` (3,822), `omop_fallback` (6,508).
**These row-level files must never be uploaded, pasted, committed, emailed,
or externally mirrored.** Their exact source/target content remains only
in the institutional environment. No RF/XGBoost models or importance
analyses have yet run.

**Next local modeling gate (not executed yet):**

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q && \
python -m pcornet_omop_validation.study.postfreeze_model_interpretability \
  --pcornet-full results/private_inputs/pcornet_full.parquet \
  --pcornet-shared results/private_inputs/pcornet_shared.parquet \
  --omop-frozen results/private_inputs/omop_frozen.parquet \
  --omop-fallback results/private_inputs/omop_fallback.parquet \
  --output results/postfreeze_interpretability/aggregate_results.json \
  --max-eval 500 --permutation-repeats 20 \
  --validate-only
```

**Do not upload the Parquet files or the protected aggregate JSON.**
After a clean validation-only run, execute the identical command
**without** `--validate-only`. The second command fits four populations
x two algorithms x six/five-feature model variants. Permutation
importance uses complete held-out evaluation sets; SHAP stays bounded.
The private aggregate report contains descriptive comparisons and
software versions, but is **not publication-cleared**. It must be
reviewed for scientific, statistical, and institutional disclosure
adequacy before code-generated figures or manuscript claims.

**Post-extraction scientific and disclosure safeguards (new):**
The original model engine measured its "primary" held-out AUROC
permutation importance on a sample capped at 500 rows. The locked
protocol calls for importance on the held-out population, reserving a
fixed bounded test sample for SHAP. That mismatch was corrected:
permutation importance now uses each full held-out test set, with a
separate all-matched-shared-test comparison, while SHAP uses at most
500 deterministically sampled rows and one common source-based
training-reference background. The selection comparison uses **matched
evaluation patients** to reduce confounding by evaluation cohort
composition. The four main contrasts (including full end-to-end) now
receive explicit right-minus-left importance and rank differences.
These are descriptive **model-attribution contrasts, not causal
effects** or uncertainty-adjusted hypothesis tests.

The modeling CLI additionally requires the four exact private Parquet
paths, mode-0700 parent and owner-only mode-0600 input files, and
creates a mode-0600, **create-only** aggregate file under the ignored
mode-0700 `results/postfreeze_interpretability/` directory. It
refuses a preexisting result and suppresses detailed errors that could
expose clinical details. `--validate-only` reads and validates all
four private inputs, index dates, labels, splits and output path without
fitting a model or writing an output. Synthetic unit tests were added,
but the updated code **has not yet passed institutional pytest or been
executed on the protected extracts**. Run the new tests and
`--validate-only` before any model fitting; preserve all protected
files if either fails.

**Original protected exporter instructions (historical, completed on 2026-11-01):**

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
