# Parse one SQL Server COUNT_BIG() result from DatabaseConnector::querySql.
#
# SQL Server / JDBC may preserve, lowercase or uppercase the SQL column alias.
# Avoid relying on result$N (or result$n), and reject malformed/missing counts.
# This helper has no database connections or side effects.
read_postfreeze_count_result <- function(result, table_name) {
  if (!is.data.frame(result) || nrow(result) != 1L || ncol(result) != 1L) {
    stop(sprintf(
      "Frozen CDM count query for %s returned unexpected result dimensions",
      table_name
    ))
  }
  count <- suppressWarnings(as.numeric(result[[1L]][[1L]]))
  if (length(count) != 1L || !is.finite(count) ||
      count < 0 || count != floor(count)) {
    stop(sprintf(
      "Frozen CDM count query for %s returned an invalid count",
      table_name
    ))
  }
  count
}


# A missing CDM_SOURCE row is a data-provenance finding. DQD requires this
# record and MUST NOT proceed. Achilles can characterize the other frozen
# tables using an explicitly supplied sourceName/cdmVersion, but its outputs
# cannot be interpreted as resolution of the provenance deficiency.
check_postfreeze_cdm_source <- function(row_count, tool) {
  count <- suppressWarnings(as.numeric(row_count))
  if (length(count) != 1L || !is.finite(count) ||
      count < 0 || count != floor(count)) {
    stop("Invalid frozen CDM_SOURCE row count")
  }
  if (length(tool) != 1L || is.na(tool) || !(tool %in% c("dqd", "achilles"))) {
    stop("Unsupported OHDSI QC tool")
  }
  if (count == 0) {
    if (tool == "dqd") {
      stop(paste(
        "DQD blocked: frozen CDM_SOURCE is empty.",
        "Do not change the frozen publication database.",
        "Use documented metadata provenance on a separately restored analysis copy."
      ))
    }
    message(paste(
      "WARNING: frozen CDM_SOURCE has zero records.",
      "Achilles is exploratory characterization and does not resolve this metadata gap."
    ))
  }
  invisible(count)
}
