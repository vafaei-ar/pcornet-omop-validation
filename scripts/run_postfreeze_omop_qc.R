#!/usr/bin/env Rscript
# Post-freeze standard OMOP QC. Runs only in an authorized institution environment.
# DQD is read-only. Achilles writes to separate designated results/scratch schemas.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L || !(args[[1]] %in% c("dqd", "achilles"))) {
  stop("Usage: Rscript scripts/run_postfreeze_omop_qc.R dqd|achilles")
}
tool <- args[[1]]
required_env <- function(name) {
  value <- Sys.getenv(name, unset = "")
  if (!nzchar(value)) stop(sprintf("Required environment variable is missing: %s", name))
  value
}
validate_schema <- function(name) {
  value <- required_env(name)
  if (!grepl("^[A-Za-z][A-Za-z0-9_]*[.][A-Za-z][A-Za-z0-9_]*$", value)) {
    stop(sprintf("%s must be a fully qualified SQL Server 'database.schema' identifier", name))
  }
  value
}
if (required_env("OHDSI_QC_CONFIRM") != "RUN_POSTFREEZE_QC_ON_FROZEN_TARGET") {
  stop("Explicit OHDSI_QC_CONFIRM safety acknowledgement is required")
}
cdm_schema <- validate_schema("OHDSI_CDM_SCHEMA")
result_schema <- validate_schema("OHDSI_RESULTS_SCHEMA")
scratch_schema <- validate_schema("OHDSI_SCRATCH_SCHEMA")
if (cdm_schema %in% c(result_schema, scratch_schema)) {
  stop("The frozen CDM schema must be distinct from results and scratch schemas")
}
if (result_schema == scratch_schema) {
  stop("Use separate Achilles results and scratch schemas")
}
parts <- function(value) strsplit(value, ".", fixed = TRUE)[[1]]
cdm_parts <- parts(cdm_schema)
results_parts <- parts(result_schema)
scratch_parts <- parts(scratch_schema)
if (cdm_parts[[1]] == results_parts[[1]] ||
    cdm_parts[[1]] == scratch_parts[[1]]) {
  stop("QC results/scratch MUST be in a different database from the frozen CDM")
}
if (results_parts[[1]] != scratch_parts[[1]]) {
  stop("QC results and scratch must be separate schemas in the same QC database")
}
expected_database <- required_env("OHDSI_DB_NAME")
if (!grepl("^[A-Za-z][A-Za-z0-9_]*$", expected_database) ||
    expected_database != cdm_parts[[1]]) {
  stop("OHDSI_DB_NAME must exactly match the frozen CDM database")
}
tls_trust <- required_env("OHDSI_TLS_TRUST_SERVER_CERTIFICATE")
if (!(tls_trust %in% c("true", "false"))) {
  stop("OHDSI_TLS_TRUST_SERVER_CERTIFICATE must be true or false")
}
if (tolower(required_env("OHDSI_DB_USER")) == "sa") {
  stop("Refusing to run standard QC with SA; use a dedicated restricted account")
}
for (package in c("DatabaseConnector", "jsonlite", if (tool == "dqd") "DataQualityDashboard" else "Achilles")) {
  if (!requireNamespace(package, quietly = TRUE)) {
    stop(sprintf("R package '%s' is not installed; install and pin it before running", package))
  }
}
cdm_version <- "5.4"  # Required argument for this frozen OMOP v5.4.2 target
tool_package <- if (tool == "dqd") "DataQualityDashboard" else "Achilles"
expected_package_version <- required_env("OHDSI_QC_EXPECTED_PACKAGE_VERSION")
installed_version <- as.character(utils::packageVersion(tool_package))
if (installed_version != expected_package_version) {
  stop(sprintf("Package version mismatch: expected %s, installed %s", expected_package_version, installed_version))
}
dbms <- Sys.getenv("OHDSI_DBMS", "sql server")
if (dbms != "sql server") stop("This project is pinned to SQL Server")
connection_details <- DatabaseConnector::createConnectionDetails(
  dbms = dbms,
  server = required_env("OHDSI_DB_SERVER"), # SQL Server hostname only (not hostname/database)
  user = required_env("OHDSI_DB_USER"),
  password = required_env("OHDSI_DB_PASSWORD"),
  extraSettings = paste0("databaseName=", expected_database,
                         ";encrypt=true;trustServerCertificate=", tls_trust),
  pathToDriver = required_env("OHDSI_JDBC_DRIVER_PATH")
)
output_folder <- file.path("results", "postfreeze_standard_qc", tool)
dir.create(output_folder, recursive = TRUE, showWarnings = FALSE)
# COUNT_BIG column aliases can be normalized differently across JDBC drivers.
# Keep snapshot parsing identical for both pre- and post-analysis checks.
source(file.path("scripts", "postfreeze_qc_count_helpers.R"), local = TRUE)
qualified <- function(schema, table) {
  parts <- strsplit(schema, ".", fixed = TRUE)[[1]]
  sprintf("[%s].[%s].[%s]", parts[[1]], parts[[2]], table)
}
snapshot <- function(details) {
  connection <- DatabaseConnector::connect(details)
  on.exit(DatabaseConnector::disconnect(connection))
  tables <- c("person", "visit_occurrence", "condition_occurrence",
              "drug_exposure", "measurement", "observation", "death",
              "cdm_source")
  counts <- lapply(tables, function(tbl) {
    sql <- sprintf("SELECT COUNT_BIG(*) AS n FROM %s", qualified(cdm_schema, tbl))
    result <- DatabaseConnector::querySql(connection, sql)
    read_postfreeze_count_result(result, tbl)
  })
  names(counts) <- tables
  counts
}
# Independent database identity/privilege check. Prior read-only preflight is
# not sufficient authorization, because environment variables may change.
validate_access <- function(details) {
  connection <- DatabaseConnector::connect(details)
  on.exit(DatabaseConnector::disconnect(connection), add = TRUE)
  roles <- DatabaseConnector::querySql(
    connection,
    paste(
      "SELECT DB_NAME() AS connected_db,",
      "IS_SRVROLEMEMBER('sysadmin') AS sysadmin_flag,",
      "IS_SRVROLEMEMBER('securityadmin') AS securityadmin_flag,",
      "IS_MEMBER('db_owner') AS db_owner_flag,",
      "IS_MEMBER('db_ddladmin') AS ddl_admin_flag,",
      "IS_MEMBER('db_datawriter') AS datawriter_flag"
    )
  )
  names(roles) <- toupper(names(roles))
  if (nrow(roles) != 1L ||
      tolower(as.character(roles$CONNECTED_DB[[1]])) != tolower(expected_database)) {
    stop("Refusing QC: connected SQL Server database identity is wrong")
  }
  for (flag in c("SYSADMIN_FLAG", "SECURITYADMIN_FLAG", "DB_OWNER_FLAG",
                 "DDL_ADMIN_FLAG", "DATAWRITER_FLAG")) {
    value <- suppressWarnings(as.integer(roles[[flag]][[1]]))
    if (is.na(value) || value != 0L) {
      stop(paste("Refusing QC: privileged or unknown SQL Server role", flag))
    }
  }
  schema <- cdm_parts[[2]]
  for (tbl in c("person", "visit_occurrence", "condition_occurrence",
                "drug_exposure", "measurement", "observation", "death")) {
    query <- sprintf(
      paste0("SELECT ",
             "HAS_PERMS_BY_NAME('%s.%s','OBJECT','INSERT') AS can_insert,",
             "HAS_PERMS_BY_NAME('%s.%s','OBJECT','UPDATE') AS can_update,",
             "HAS_PERMS_BY_NAME('%s.%s','OBJECT','DELETE') AS can_delete,",
             "HAS_PERMS_BY_NAME('%s.%s','OBJECT','ALTER') AS can_alter"),
      schema, tbl, schema, tbl, schema, tbl, schema, tbl
    )
    rights <- DatabaseConnector::querySql(connection, query)
    names(rights) <- toupper(names(rights))
    if (nrow(rights) != 1L) stop("Unexpected permission query result")
    for (permission in c("CAN_INSERT", "CAN_UPDATE", "CAN_DELETE", "CAN_ALTER")) {
      value <- suppressWarnings(as.integer(rights[[permission]][[1]]))
      if (is.na(value) || value != 0L) {
        stop(paste("Refusing QC: write privilege or unknown state on CDM", tbl, permission))
      }
    }
  }
  message("QC login privilege gate: PASS (sampled CDM tables)")
}
validate_access(connection_details)

expected_frozen_counts <- c(
  person = 27089,
  visit_occurrence = 1510957,
  condition_occurrence = 7315572
)
validate_frozen_anchors <- function(counts) {
  actual <- vapply(names(expected_frozen_counts),
                   function(tbl) as.numeric(counts[[tbl]]), numeric(1))
  if (!identical(unname(actual), unname(as.numeric(expected_frozen_counts)))) {
    stop("Frozen publication row-count anchors mismatch: QC execution blocked")
  }
}
before <- snapshot(connection_details)
validate_frozen_anchors(before)
check_postfreeze_cdm_source(before$cdm_source, tool)
if (tool == "dqd") {
  DataQualityDashboard::executeDqChecks(
    connectionDetails = connection_details,
    cdmDatabaseSchema = cdm_schema,
    resultsDatabaseSchema = result_schema,
    vocabDatabaseSchema = cdm_schema,
    cdmSourceName = "Frozen PCORnet-to-OMOP validation",
    cdmVersion = cdm_version,
    numThreads = 1,
    checkLevels = c("TABLE", "FIELD", "CONCEPT"),
    checkSeverity = c("fatal", "convention", "characterization"),
    outputFolder = output_folder,
    outputFile = "dqd_results.json",
    writeToTable = FALSE,
    verboseMode = FALSE
  )
} else {
  Achilles::achilles(
    connectionDetails = connection_details,
    cdmDatabaseSchema = cdm_schema,
    resultsDatabaseSchema = result_schema,
    scratchDatabaseSchema = scratch_schema,
    vocabDatabaseSchema = cdm_schema,
    sourceName = "Frozen PCORnet-to-OMOP validation",
    cdmVersion = cdm_version,
    numThreads = 1,
    smallCellCount = 10,
    createTable = TRUE,
    createIndices = FALSE,
    dropScratchTables = TRUE,
    defaultAnalysesOnly = TRUE,
    outputFolder = output_folder,
    verboseMode = TRUE
  )
}
after <- snapshot(connection_details)
validate_frozen_anchors(after)
if (!identical(before, after)) {
  stop("FROZEN TARGET ROW COUNT CHANGED; investigate immediately before using outputs")
}
version <- if (tool == "dqd") "DataQualityDashboard" else "Achilles"
manifest <- list(
  status = "postfreeze_standard_qc_execution_complete",
  analysis_role = "exploratory_standard_OMOP_target_only_QC",
  tool = tool,
  tool_version = as.character(utils::packageVersion(version)),
  database_connector_version = as.character(utils::packageVersion("DatabaseConnector")),
  target_cdm_version = "5.4.2",
  target_cdm_counts_preserved = TRUE,
  frozen_cdm_source_row_count = as.numeric(after$cdm_source),
  frozen_cdm_source_populated = as.numeric(after$cdm_source) > 0,
  cdm_source_metadata_gap_unresolved = as.numeric(after$cdm_source) == 0,
  frozen_etl_sha = "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
  recorded_at_utc = format(Sys.time(), tz = "UTC", usetz = TRUE),
  output_local_only = TRUE,
  disclosure_review_required_before_publication = TRUE,
  achilles_small_cell_threshold = if (tool == "achilles") 10L else NULL
)
jsonlite::write_json(manifest, file.path(output_folder, "execution_manifest.json"),
                     auto_unbox = TRUE, pretty = TRUE)
message("Complete: ", tool, "; results are LOCAL and must be disclosure-reviewed.")
