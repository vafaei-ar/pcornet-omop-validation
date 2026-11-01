#!/usr/bin/env Rscript
# Read-only permissions audit of the dedicated post-freeze OHDSI QC login.
# Connection details come exclusively from the existing Python preflight wrapper.
# This script issues SELECT only; no DQD, Achilles, DML, DDL or data export.
needed <- function(key) {
  value <- Sys.getenv(key, unset = "")
  if (!nzchar(value)) stop(sprintf("Missing required environment setting: %s", key))
  value
}
identifier <- function(key) {
  value <- needed(key)
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*$", value)) {
    stop(sprintf("Invalid SQL identifier: %s", key))
  }
  value
}
as_flag <- function(row, name, expected, description) {
  value <- suppressWarnings(as.integer(row[[name]][[1]]))
  if (length(value) != 1L || is.na(value) || value != expected) {
    stop(sprintf("QC ACCESS AUDIT FAILED: %s (expected %d; found %s)",
                 description, expected,
                 if (length(value) != 1L || is.na(value)) "UNKNOWN" else as.character(value)))
  }
}
row_query <- function(conn, sql) {
  result <- DatabaseConnector::querySql(conn, sql)
  names(result) <- toupper(names(result))
  if (nrow(result) != 1L) stop("QC ACCESS AUDIT FAILED: SQL result must have one row")
  result
}
quote_identifier <- function(x) sprintf("[%s]", gsub("]", "]]", x, fixed = TRUE))
make_details <- function(database, server, login, password, driver, trust) {
  DatabaseConnector::createConnectionDetails(
    dbms = "sql server", server = server, user = login, password = password,
    extraSettings = paste0("databaseName=", database,
                           ";encrypt=true;trustServerCertificate=", trust),
    pathToDriver = driver
  )
}
audit_identity_and_roles <- function(conn, database, login) {
  result <- row_query(conn, paste(
    "SELECT DB_NAME() AS connected_db, ORIGINAL_LOGIN() AS original_login,",
    "IS_SRVROLEMEMBER('sysadmin') AS sysadmin_flag,",
    "IS_SRVROLEMEMBER('securityadmin') AS securityadmin_flag,",
    "IS_MEMBER('db_owner') AS dbowner_flag,",
    "IS_MEMBER('db_ddladmin') AS ddladmin_flag,",
    "IS_MEMBER('db_datawriter') AS datawriter_flag,",
    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL') AS control_db_flag,"
    ,"HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER ANY SCHEMA') AS alter_schema_flag"
  ))
  if (!identical(tolower(as.character(result$CONNECTED_DB[[1]])), tolower(database))) {
    stop("QC ACCESS AUDIT FAILED: connected to the wrong database")
  }
  if (!identical(tolower(as.character(result$ORIGINAL_LOGIN[[1]])), tolower(login))) {
    stop("QC ACCESS AUDIT FAILED: authenticated SQL login mismatch")
  }
  for (field in c("SYSADMIN_FLAG", "SECURITYADMIN_FLAG", "DBOWNER_FLAG",
                  "DDLADMIN_FLAG", "DATAWRITER_FLAG", "CONTROL_DB_FLAG",
                  "ALTER_SCHEMA_FLAG")) {
    as_flag(result, field, 0L, paste(database, field))
  }
}
audit_frozen <- function(conn, database, schema) {
  r <- row_query(conn, sprintf(paste(
    "SELECT HAS_PERMS_BY_NAME('%s','SCHEMA','SELECT') AS can_select,",
    "HAS_PERMS_BY_NAME('%s','SCHEMA','INSERT') AS can_insert,",
    "HAS_PERMS_BY_NAME('%s','SCHEMA','UPDATE') AS can_update,",
    "HAS_PERMS_BY_NAME('%s','SCHEMA','DELETE') AS can_delete,",
    "HAS_PERMS_BY_NAME('%s','SCHEMA','ALTER') AS can_alter,",
    "HAS_PERMS_BY_NAME('%s','SCHEMA','CONTROL') AS can_control,",
    "HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE') AS can_create_table"),
    schema, schema, schema, schema, schema, schema))
  as_flag(r, "CAN_SELECT", 1L, "frozen schema SELECT")
  for (f in c("CAN_INSERT", "CAN_UPDATE", "CAN_DELETE", "CAN_ALTER",
              "CAN_CONTROL", "CAN_CREATE_TABLE")) {
    as_flag(r, f, 0L, paste("frozen schema or database", f))
  }

  sql <- sprintf(paste(
    "SELECT t.name AS table_name FROM sys.tables t",
    "INNER JOIN sys.schemas s ON t.schema_id=s.schema_id",
    "WHERE s.name='%s' ORDER BY t.name"), schema)
  tables <- DatabaseConnector::querySql(conn, sql)
  names(tables) <- toupper(names(tables))
  table_names <- as.character(tables$TABLE_NAME)
  if (!all(c("person", "visit_occurrence", "condition_occurrence") %in%
           tolower(table_names))) {
    stop("QC ACCESS AUDIT FAILED: missing required frozen tables")
  }
  for (table_name in table_names) {
    object <- paste(quote_identifier(schema), quote_identifier(table_name), sep = ".")
    sql_object <- gsub("'", "''", object, fixed = TRUE)
    sql <- sprintf(paste(
      "SELECT HAS_PERMS_BY_NAME('%s','OBJECT','SELECT') AS can_select,",
      "HAS_PERMS_BY_NAME('%s','OBJECT','INSERT') AS can_insert,",
      "HAS_PERMS_BY_NAME('%s','OBJECT','UPDATE') AS can_update,",
      "HAS_PERMS_BY_NAME('%s','OBJECT','DELETE') AS can_delete,",
      "HAS_PERMS_BY_NAME('%s','OBJECT','ALTER') AS can_alter,",
      "HAS_PERMS_BY_NAME('%s','OBJECT','CONTROL') AS can_control"),
      sql_object, sql_object, sql_object, sql_object, sql_object, sql_object)
    rights <- row_query(conn, sql)
    as_flag(rights, "CAN_SELECT", 1L, paste("frozen table", table_name, "SELECT"))
    for (f in c("CAN_INSERT", "CAN_UPDATE", "CAN_DELETE", "CAN_ALTER",
                "CAN_CONTROL")) {
      as_flag(rights, f, 0L, paste("frozen table", table_name, f))
    }
  }
  counts <- c(person = 27089, visit_occurrence = 1510957,
              condition_occurrence = 7315572)
  for (table_name in names(counts)) {
    sql <- sprintf("SELECT COUNT_BIG(*) AS n FROM %s.%s.%s",
                   quote_identifier(database), quote_identifier(schema),
                   quote_identifier(table_name))
    result <- row_query(conn, sql)
    observed <- as.numeric(result$N[[1]])
    if (!is.finite(observed) || observed != counts[[table_name]]) {
      stop(sprintf("QC ACCESS AUDIT FAILED: frozen count mismatch for %s", table_name))
    }
  }
  cat(sprintf("Frozen schema: SELECT granted; writes denied on %d tables; anchor counts PASS\n",
              length(table_names)))
}
audit_qc_outputs <- function(conn, database) {
  permissions <- row_query(conn, paste(
    "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE') AS can_create_table,",
    "HAS_PERMS_BY_NAME('dbo','SCHEMA','ALTER') AS can_alter_dbo,",
    "HAS_PERMS_BY_NAME('dbo','SCHEMA','CONTROL') AS can_control_dbo,",
    "HAS_PERMS_BY_NAME('dbo','SCHEMA','INSERT') AS can_insert_dbo,",
    "HAS_PERMS_BY_NAME('dbo','SCHEMA','UPDATE') AS can_update_dbo,",
    "HAS_PERMS_BY_NAME('dbo','SCHEMA','DELETE') AS can_delete_dbo"
  ))
  as_flag(permissions, "CAN_CREATE_TABLE", 1L, "QC database CREATE TABLE")
  for (f in c("CAN_ALTER_DBO", "CAN_CONTROL_DBO", "CAN_INSERT_DBO",
              "CAN_UPDATE_DBO", "CAN_DELETE_DBO")) {
    as_flag(permissions, f, 0L, paste("QC database dbo", f))
  }
  for (schema in c("qc_results", "qc_scratch")) {
    sql <- sprintf(paste(
      "SELECT HAS_PERMS_BY_NAME('%s','SCHEMA','ALTER') AS can_alter,",
      "HAS_PERMS_BY_NAME('%s','SCHEMA','SELECT') AS can_select,",
      "HAS_PERMS_BY_NAME('%s','SCHEMA','INSERT') AS can_insert,",
      "HAS_PERMS_BY_NAME('%s','SCHEMA','UPDATE') AS can_update,",
      "HAS_PERMS_BY_NAME('%s','SCHEMA','DELETE') AS can_delete"),
      schema, schema, schema, schema, schema)
    r <- row_query(conn, sql)
    for (f in c("CAN_ALTER", "CAN_SELECT", "CAN_INSERT", "CAN_UPDATE", "CAN_DELETE")) {
      as_flag(r, f, 1L, paste(schema, f))
    }
  }
  existing <- row_query(conn, paste(
    "SELECT COUNT_BIG(*) AS n FROM sys.tables t",
    "INNER JOIN sys.schemas s ON t.schema_id=s.schema_id",
    "WHERE s.name IN ('qc_results','qc_scratch')"
  ))
  n <- as.numeric(existing$N[[1]])
  if (!is.finite(n)) stop("QC ACCESS AUDIT FAILED: cannot count existing QC tables")
  cat("QC output database:", database, "\n")
  cat("QC output schema CREATE/ALTER/SELECT/INSERT/UPDATE/DELETE: PASS\n")
  cat("Existing QC tables:", format(n, scientific = FALSE), "\n")
  if (n > 0) cat("WARNING: inspect existing QC tables before any Achilles createTable run\n")
}
main <- function() {
  if (!requireNamespace("DatabaseConnector", quietly = TRUE)) {
    stop("Missing DatabaseConnector in configured R library")
  }
  database <- identifier("OHDSI_DB_NAME")
  if (!identical(database, "OMOP_VALIDATED_A")) {
    stop("QC access audit is locked to the verified frozen publication database")
  }
  schema <- identifier("OHDSI_TARGET_SCHEMA")
  if (schema != "dbo") stop("QC audit expects frozen schema dbo")
  login <- needed("OHDSI_DB_USER")
  if (login != "ohdsi_postfreeze_qc") {
    stop("QC ACCESS AUDIT BLOCKED: restricted OHDSI login required")
  }
  password <- needed("OHDSI_DB_PASSWORD")
  server <- needed("OHDSI_DB_SERVER")
  driver <- needed("OHDSI_JDBC_DRIVER_PATH")
  trust <- needed("OHDSI_TLS_TRUST_SERVER_CERTIFICATE")
  if (!(trust %in% c("true", "false"))) {
    stop("QC ACCESS AUDIT BLOCKED: invalid TLS setting")
  }
  output_db <- paste0(database, "_postfreeze_qc")
  source_details <- make_details(database, server, login, password, driver, trust)
  source_conn <- DatabaseConnector::connect(source_details)
  on.exit(DatabaseConnector::disconnect(source_conn), add = TRUE)
  audit_identity_and_roles(source_conn, database, login)
  audit_frozen(source_conn, database, schema)

  results_details <- make_details(output_db, server, login, password, driver, trust)
  results_conn <- DatabaseConnector::connect(results_details)
  on.exit(DatabaseConnector::disconnect(results_conn), add = TRUE)
  audit_identity_and_roles(results_conn, output_db, login)
  audit_qc_outputs(results_conn, output_db)
  cat("QC ACCESS AUDIT: PASS (SELECT-only, no database changes)\n")
}
main()
