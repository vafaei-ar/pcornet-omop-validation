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
