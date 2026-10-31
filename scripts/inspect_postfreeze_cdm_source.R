#!/usr/bin/env Rscript
# Read-only metadata diagnostic for blocked DQD initialization on the
# frozen OMOP 5.4.2 publication target. No DDL/DML, values or PHI exposed.
need <- function(key) {
  value <- Sys.getenv(key, unset = "")
  if (!nzchar(value)) stop(sprintf("Missing required env setting: %s", key))
  value
}
identifier <- function(key) {
  value <- need(key)
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*$", value)) {
    stop(sprintf("Invalid SQL Server identifier in %s", key))
  }
  value
}
one_count <- function(connection, sql, name) {
  value <- DatabaseConnector::querySql(connection, sql)
  if (!is.data.frame(value) || nrow(value) != 1L || ncol(value) != 1L) {
    stop(sprintf("Unexpected aggregate shape: %s", name))
  }
  number <- suppressWarnings(as.numeric(value[[1L]][[1L]]))
  if (length(number) != 1L || !is.finite(number) ||
      number < 0 || number != floor(number)) {
    stop(sprintf("Invalid aggregate result: %s", name))
  }
  number
}
main <- function() {
  if (!requireNamespace("DatabaseConnector", quietly = TRUE)) {
    stop("DatabaseConnector is not installed in R_LIBS_USER")
  }
  db <- identifier("OHDSI_DB_NAME")
  schema <- identifier("OHDSI_TARGET_SCHEMA")
  if (db != "OMOP_VALIDATED_A" || schema != "dbo") {
    stop("Refusing to inspect any database other than the frozen target")
  }
  login <- need("OHDSI_DB_USER")
  if (login != "ohdsi_postfreeze_qc") {
    stop("Restricted QC principal required")
  }
  trust <- need("OHDSI_TLS_TRUST_SERVER_CERTIFICATE")
  if (!(trust %in% c("true", "false"))) {
    stop("Invalid TLS trust setting")
  }
  connection_details <- DatabaseConnector::createConnectionDetails(
    dbms = "sql server", server = need("OHDSI_DB_SERVER"),
    user = login, password = need("OHDSI_DB_PASSWORD"),
    extraSettings = paste0("databaseName=", db,
                           ";encrypt=true;trustServerCertificate=", trust),
    pathToDriver = need("OHDSI_JDBC_DRIVER_PATH")
  )
  connection <- DatabaseConnector::connect(connection_details)
  on.exit(DatabaseConnector::disconnect(connection), add = TRUE)
  session <- DatabaseConnector::querySql(
    connection, "SELECT DB_NAME() AS connected_database, ORIGINAL_LOGIN() AS original_login"
  )
  if (nrow(session) != 1L ||
      tolower(as.character(session[[1L]][[1L]])) != tolower(db) ||
      tolower(as.character(session[[2L]][[1L]])) != tolower(login)) {
    stop("Frozen database or login identity mismatch")
  }
  prefix <- sprintf("[%s].[%s]", db, schema)
  columns <- DatabaseConnector::querySql(
    connection,
    sprintf(paste(
      "SELECT COLUMN_NAME FROM [%s].INFORMATION_SCHEMA.COLUMNS",
      "WHERE TABLE_SCHEMA = '%s' AND TABLE_NAME = 'cdm_source'"
    ), db, schema)
  )
  if (ncol(columns) != 1L) stop("Unexpected CDM_SOURCE column metadata")
  available <- tolower(as.character(columns[[1L]]))
  if (length(available) == 0L) {
    cat("CDM_SOURCE TABLE: MISSING or not visible; DQD prerequisite unavailable\n")
    cat("DIAGNOSTIC COMPLETE: SELECT only, no database changes\n")
    return(invisible(NULL))
  }
  fields <- c(
    "cdm_source_name", "cdm_source_abbreviation", "cdm_holder",
    "source_description", "source_documentation_reference",
    "cdm_etl_reference", "source_release_date", "cdm_release_date",
    "cdm_version", "cdm_version_concept_id", "vocabulary_version"
  )
  missing <- setdiff(fields, available)
  cat("CDM_SOURCE TABLE: EXISTS\n")
  cat("CDM_SOURCE expected fields present:",
      length(fields) - length(missing), "of", length(fields), "\n")
  if (length(missing)) {
    cat("CDM_SOURCE absent fields:", paste(missing, collapse = ", "), "\n")
  }
  table <- sprintf("%s.[cdm_source]", prefix)
  n <- one_count(connection, sprintf("SELECT COUNT_BIG(*) AS n FROM %s", table),
                 "CDM_SOURCE row count")
  cat("CDM_SOURCE row count:", format(n, scientific = FALSE), "\n")
  if (n == 0) {
    cat("DQD BLOCKER: CDM_SOURCE IS EMPTY\n")
  } else if (n != 1) {
    cat("DQD METADATA REVIEW: more than one CDM_SOURCE row; identify source selection locally\n")
  }
  if (n > 0) {
    for (field in intersect(fields, available)) {
      quoted <- sprintf("[%s]", field)
      present_expr <- if (field %in% c("source_release_date", "cdm_release_date")) {
        sprintf("%s IS NOT NULL", quoted)
      } else if (field == "cdm_version_concept_id") {
        sprintf("TRY_CONVERT(bigint, %s) > 0", quoted)
      } else {
        sprintf("NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(max), %s))), '') IS NOT NULL", quoted)
      }
      count <- one_count(
        connection,
        sprintf("SELECT COUNT_BIG(*) AS n FROM %s WHERE %s", table, present_expr),
        paste("populated", field)
      )
      cat("Field", field, "populated rows:", format(count, scientific = FALSE),
          "of", format(n, scientific = FALSE), "\n")
    }
    cat("Field contents withheld: this diagnostic reports presence only.\n")
  }
  if (nrow(columns) > 0L) {
    vocab <- one_count(
      connection,
      sprintf(paste(
        "SELECT COUNT_BIG(*) AS n FROM %s.[vocabulary]",
        "WHERE vocabulary_id = 'None' AND",
        "NULLIF(LTRIM(RTRIM(vocabulary_version)), '') IS NOT NULL"
      ), prefix),
      "vocabulary version reference count"
    )
    cat("Vocabulary metadata row(s) with ID None and a version:",
        format(vocab, scientific = FALSE), "\n")
  }
  cat("DIAGNOSTIC COMPLETE: SELECT only, no database changes\n")
}
main()
