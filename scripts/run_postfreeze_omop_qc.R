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
for (package in c("DatabaseConnector", "jsonlite", if (tool == "dqd") "DataQualityDashboard" else "Achilles")) {
  if (!requireNamespace(package, quietly = TRUE)) {
    stop(sprintf("R package '%s' is not installed; install and pin it before running", package))
  }
}
cdm_version <- "5.4"  # Required argument for this frozen OMOP v5.4.2 target
dbms <- Sys.getenv("OHDSI_DBMS", "sql server")
if (dbms != "sql server") stop("This project is pinned to SQL Server")
connection_details <- DatabaseConnector::createConnectionDetails(
  dbms = dbms,
  server = required_env("OHDSI_DB_SERVER"), # SQL Server 'hostname/database'
  user = required_env("OHDSI_DB_USER"),
  password = required_env("OHDSI_DB_PASSWORD"),
  pathToDriver = required_env("OHDSI_JDBC_DRIVER_PATH")
)
output_folder <- file.path("results", "postfreeze_standard_qc", tool)
dir.create(output_folder, recursive = TRUE, showWarnings = FALSE)
qualified <- function(schema, table) {
  parts <- strsplit(schema, ".", fixed = TRUE)[[1]]
  sprintf("[%s].[%s].[%s]", parts[[1]], parts[[2]], table)
}
snapshot <- function(details) {
  connection <- DatabaseConnector::connect(details)
  on.exit(DatabaseConnector::disconnect(connection))
  tables <- c("person", "visit_occurrence", "condition_occurrence",
              "drug_exposure", "measurement", "observation", "death")
  counts <- lapply(tables, function(tbl) {
    sql <- sprintf("SELECT COUNT_BIG(*) AS n FROM %s", qualified(cdm_schema, tbl))
    n <- DatabaseConnector::querySql(connection, sql)$N[[1]]
    as.numeric(n)
  })
  names(counts) <- tables
  counts
}
before <- snapshot(connection_details)
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
  frozen_etl_sha = "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
  recorded_at_utc = format(Sys.time(), tz = "UTC", usetz = TRUE),
  output_local_only = TRUE,
  disclosure_review_required_before_publication = TRUE,
  achilles_small_cell_threshold = if (tool == "achilles") 10L else NULL
)
jsonlite::write_json(manifest, file.path(output_folder, "execution_manifest.json"),
                     auto_unbox = TRUE, pretty = TRUE)
message("Complete: ", tool, "; results are LOCAL and must be disclosure-reviewed.")
