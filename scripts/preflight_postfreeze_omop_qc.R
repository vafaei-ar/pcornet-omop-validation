#!/usr/bin/env Rscript
# Read-only SQL Server/JDBC preflight for the frozen publication OMOP target.
# Inputs are local environment variables supplied by the Python wrapper.
# No ETL, DQD, Achilles, DML, DDL, data export, or schema creation occurs.

need <- function(key) {
  value <- Sys.getenv(key, unset = "")
  if (!nzchar(value)) stop(paste("Missing required environment variable:", key))
  value
}

safe_identifier <- function(value, key) {
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*$", value)) {
    stop(paste("Unsafe database/schema identifier in", key))
  }
  value
}

main <- function() {
  if (!requireNamespace("DatabaseConnector", quietly = TRUE)) {
    stop("DatabaseConnector is not installed in R_LIBS_USER")
  }

  db <- safe_identifier(need("OHDSI_DB_NAME"), "OHDSI_DB_NAME")
  schema <- safe_identifier(need("OHDSI_TARGET_SCHEMA"), "OHDSI_TARGET_SCHEMA")
  driver_folder <- need("OHDSI_JDBC_DRIVER_PATH")
  if (!dir.exists(driver_folder) ||
      length(list.files(driver_folder, pattern = "\\.jar$", recursive = TRUE)) < 1L) {
    stop("SQL Server JDBC driver JAR is missing")
  }
  trust <- need("OHDSI_TLS_TRUST_SERVER_CERTIFICATE")
  if (!(trust %in% c("true", "false"))) {
    stop("OHDSI_TLS_TRUST_SERVER_CERTIFICATE must be true or false")
  }

  details <- DatabaseConnector::createConnectionDetails(
    dbms = "sql server",
    server = need("OHDSI_DB_SERVER"),
    user = need("OHDSI_DB_USER"),
    password = need("OHDSI_DB_PASSWORD"),
    extraSettings = paste0("databaseName=", db,
                           ";encrypt=true;trustServerCertificate=", trust),
    pathToDriver = driver_folder
  )
  connection <- DatabaseConnector::connect(details)
  on.exit(DatabaseConnector::disconnect(connection), add = TRUE)

  # All SQL statements are a single SELECT. The connection must resolve to the
  # configured database; do not silently accept the login's default database.
  permission_sql <- sprintf(
    paste(
      "SELECT DB_NAME() AS connected_database,",
      "IS_SRVROLEMEMBER('sysadmin') AS sysadmin_flag,",
      "IS_MEMBER('db_owner') AS dbowner_flag,",
      "HAS_PERMS_BY_NAME('%s.person','OBJECT','UPDATE') AS can_update_person,",
      "HAS_PERMS_BY_NAME('%s.person','OBJECT','DELETE') AS can_delete_person,",
      "HAS_PERMS_BY_NAME('%s.person','OBJECT','ALTER') AS can_alter_person"
    ), schema, schema, schema
  )
  perm <- DatabaseConnector::querySql(connection, permission_sql)
  names(perm) <- toupper(names(perm))
  if (nrow(perm) != 1L) stop("Unexpected permission-query result")
  connected_db <- as.character(perm$CONNECTED_DATABASE[[1]])
  if (tolower(connected_db) != tolower(db)) {
    stop("Database identity mismatch: refusing to inspect an unintended database")
  }
  cat("JDBC connection: SUCCESS\n")
  cat("Configured target database: verified\n")
  cat("DatabaseConnector version:",
      as.character(utils::packageVersion("DatabaseConnector")), "\n")

  expected <- c(person = 27089,
                visit_occurrence = 1510957,
                condition_occurrence = 7315572)
  actual <- setNames(numeric(length(expected)), names(expected))
  for (table in names(expected)) {
    # Identifiers are strictly validated above; table names come from constants.
    sql <- sprintf("SELECT COUNT_BIG(*) AS n FROM [%s].[%s].[%s]",
                   db, schema, table)
    result <- DatabaseConnector::querySql(connection, sql)
    if (nrow(result) != 1L || ncol(result) != 1L) {
      stop(paste("Unexpected row-count query result for", table))
    }
    count <- as.numeric(result[[1]][[1]])
    if (!is.finite(count)) stop(paste("Invalid count for", table))
    actual[[table]] <- count
    cat(table, ": observed", format(count, scientific = FALSE),
        "; frozen reference", format(expected[[table]], scientific = FALSE), "\n")
  }
  if (!identical(as.numeric(actual), as.numeric(expected))) {
    stop("FROZEN TARGET ANCHOR MISMATCH: do NOT run DQD or Achilles")
  }
  cat("Frozen target count anchors: PASS\n")

  read_privilege <- function(column) {
    raw <- suppressWarnings(as.integer(perm[[column]][[1]]))
    if (is.na(raw)) "UNKNOWN" else if (raw == 1L) "YES" else "NO"
  }
  risk <- c(
    sysadmin = read_privilege("SYSADMIN_FLAG"),
    db_owner = read_privilege("DBOWNER_FLAG"),
    update_person = read_privilege("CAN_UPDATE_PERSON"),
    delete_person = read_privilege("CAN_DELETE_PERSON"),
    alter_person = read_privilege("CAN_ALTER_PERSON")
  )
  cat("Current login privileges (YES/NO/UNKNOWN):\n")
  for (name in names(risk)) cat(" ", name, ":", risk[[name]], "\n")

  if (any(risk != "NO")) {
    cat("PRIVILEGE GATE: NOT CLEARED for production QC.\n")
    cat("The read-only SELECT preflight succeeded; provision a least-privilege\n")
    cat("QC login with SELECT on frozen CDM and scoped write access only to\n")
    cat("distinct Achilles results/scratch schemas before running QC.\n")
  } else {
    cat("PRIVILEGE GATE: preliminary check passed (not a complete grant audit).\n")
  }
  cat("PREFLIGHT COMPLETE: no database writes performed.\n")
}

main()
