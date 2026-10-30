"""Regression checks for the name-agnostic SQL Server COUNT_BIG snapshot parser.

No clinical database connections. The R helper tests use synthetic data and
skip cleanly on systems that do not have Rscript installed.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_qc_runner_uses_alias_independent_snapshot_parser():
    code = (ROOT / "scripts" / "run_postfreeze_omop_qc.R").read_text(
        encoding="utf-8"
    )
    assert 'source(file.path("scripts", "postfreeze_qc_count_helpers.R")' in code
    assert "read_postfreeze_count_result(result, tbl)" in code
    assert "DatabaseConnector::querySql(connection, sql)$N" not in code
    assert "validate_frozen_anchors(before)" in code
    assert "validate_frozen_anchors(after)" in code


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")
def test_count_big_parser_handles_driver_column_name_variations():
    script = r'''
        source("scripts/postfreeze_qc_count_helpers.R")
        stopifnot(
          identical(read_postfreeze_count_result(
              data.frame(n = 27089), "person"), 27089),
          identical(read_postfreeze_count_result(
              data.frame(N = 1510957), "visit_occurrence"), 1510957),
          identical(read_postfreeze_count_result(
              data.frame(row_count = 7315572), "condition_occurrence"), 7315572),
          identical(read_postfreeze_count_result(
              data.frame(any_name = 0), "death"), 0)
        )
        cat("COUNT RESULT PARSER: PASS\n")
    '''
    result = subprocess.run(
        ["Rscript", "--vanilla", "-e", script],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "COUNT RESULT PARSER: PASS" in result.stdout


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")
def test_count_big_parser_rejects_empty_multiple_and_nonnumeric_results():
    script = r'''
        source("scripts/postfreeze_qc_count_helpers.R")
        must_reject <- function(value) {
          inherits(try(read_postfreeze_count_result(value, "person"),
                       silent = TRUE), "try-error")
        }
        stopifnot(
          must_reject(data.frame(n = numeric(0))),
          must_reject(data.frame(n = c(1, 2))),
          must_reject(data.frame(n = NA_real_)),
          must_reject(data.frame(n = Inf)),
          must_reject(data.frame(n = -1)),
          must_reject(data.frame(n = 1.5)),
          must_reject(data.frame(n = "not-a-count")),
          must_reject(data.frame(a = 1, b = 1)),
          must_reject(NULL)
        )
        cat("INVALID COUNT RESULT REJECTION: PASS\n")
    '''
    result = subprocess.run(
        ["Rscript", "--vanilla", "-e", script],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "INVALID COUNT RESULT REJECTION: PASS" in result.stdout
