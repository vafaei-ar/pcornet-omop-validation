"""Synthetic tests for SELECT-only Achilles analysis-ID metadata reconciliation.

The upstream Achilles v1.7.2 SQL Server performance export joins the
metadata definitions on analysis_id + 2000000. These tests verify that
bookkeeping is classified separately from persisted data-bearing results.
No SQL connections or clinical fields are used in this test suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_postfreeze_achilles_analysis_ids.py"
SPEC = importlib.util.spec_from_file_location("achilles_id_reconciliation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

# Synthetic case matches the user's reported ID set sizes. It contains
# numbers of distinct *analysis identifiers*, not person/patient counts.
SUMMARY = (
    259, 77, 334, 2,
    294, 294, 194, 154,
    0, 0, 0,
)
PERFORMANCE = (
    194, 0,
    194, 100, 0,
    194, 0, 0,
    140, 0, 0,
)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.kind = ""

    def execute(self, sql):
        text = " ".join(sql.strip().split())
        self.conn.queries.append(text)
        assert not any(
            forbidden in text.upper()
            for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE ",
                              "DROP TABLE", "CREATE TABLE",
                              "ALTER TABLE", "TRUNCATE TABLE")
        ), "The diagnostic must issue SELECT-only queries"
        if "DB_NAME(), ORIGINAL_LOGIN()" in text:
            self.kind = "identity"
        elif "FROM sys.tables" in text:
            self.kind = "metadata"
        elif "SELECT TOP (20) u.analysis_id" in text:
            self.kind = "sample"
        elif "AS unexplained_ids" in text:
            self.kind = "performance"
        elif "AS unmatched_results_ids" in text:
            self.kind = "summary"
        else:
            raise AssertionError(f"Unexpected query: {text[:150]}")
        return self

    def fetchone(self):
        if self.kind == "identity":
            return (self.conn.database, "ohdsi_postfreeze_qc")
        if self.kind == "summary":
            return self.conn.values
        if self.kind == "performance":
            return self.conn.performance
        return None

    def fetchall(self):
        if self.kind == "metadata":
            tables = [
                ("qc_results", "ACHILLES_ANALYSIS"),
                ("qc_results", "achilles_results"),
                ("qc_results", "ACHILLES_RESULTS_DIST"),
            ]
            if self.conn.missing_analysis:
                tables = [pair for pair in tables
                          if pair[1].casefold() != "achilles_analysis"]
            if self.conn.collision:
                tables.append(("QC_RESULTS", "achilles_results"))
            return tables
        if self.kind == "sample":
            return [(2999999,)]
        return []

    def close(self):
        self.conn.closed_cursors += 1


class FakeConnection:
    def __init__(
        self, values=None, performance=None, missing_analysis=False,
        collision=False,
    ):
        self.database = "OMOP_VALIDATED_A_postfreeze_qc"
        self.queries = []
        self.closed_cursors = 0
        self.missing_analysis = missing_analysis
        self.collision = collision
        self.values = SUMMARY if values is None else values
        self.performance = PERFORMANCE if performance is None else performance

    def cursor(self):
        return FakeCursor(self)


def test_realistic_performance_offset_classified_without_patient_data(capsys):
    conn = FakeConnection()
    summary = module.reconcile(conn)
    assert summary["combined_ids"] == 334
    assert summary["definition_rows"] == 294
    assert summary["unmatched_results_ids"] == 194
    assert len(conn.queries) == 4
    assert conn.closed_cursors == len(conn.queries)
    assert all("COUNT_VALUE" not in query.upper() for query in conn.queries)
    assert all("STRATUM_" not in query.upper() for query in conn.queries)
    output = capsys.readouterr().out
    assert "RECONCILIATION: PASS" in output
    assert "194" in output
    assert "Truly unexplained stored IDs: 0" in output
    assert "Default analyses without data-bearing result IDs: 54" in output
    assert "REVIEW REQUIRED" not in output


def test_unexplained_id_is_not_mislabeled_as_performance(capsys):
    performance = (
        193, 1, 194, 100, 0, 193, 0, 0, 140, 0, 0
    )
    conn = FakeConnection(performance=performance)
    module.reconcile(conn)
    assert len(conn.queries) == 5
    output = capsys.readouterr().out
    assert "RECONCILIATION: REVIEW REQUIRED" in output
    assert "Truly unexplained stored IDs: 1" in output
    assert "2999999" in output


def test_no_performance_records_is_not_a_normal_complete_run(capsys):
    # Same default definitions but no performance IDs: investigate
    # rather than declaring the execution successfully reconciled.
    values = (194, 0, 194, 0, 294, 294, 0, 100, 0, 0, 0)
    performance = (0, 0, 194, 100, 0, 0, 0, 0, 194, 0, 0)
    conn = FakeConnection(values=values, performance=performance)
    module.reconcile(conn)
    assert len(conn.queries) == 4
    assert "REVIEW REQUIRED" in capsys.readouterr().out


def test_upstream_sql_performance_offset_preserved_in_queries():
    assert module.PERFORMANCE_ID_OFFSET == 2_000_000
    query = module.performance_namespace_sql({
        "achilles_analysis": "[qc_results].[ACHILLES_ANALYSIS]",
        "achilles_results": "[qc_results].[ACHILLES_RESULTS]",
        "achilles_results_dist": "[qc_results].[ACHILLES_RESULTS_DIST]",
    })
    assert "d.analysis_id + 2000000" in query
    assert "is_default" in query
    assert "count_value" not in query
    assert "stratum_" not in query


def test_case_mismatched_actual_table_names_are_preserved_in_selects():
    conn = FakeConnection()
    module.reconcile(conn)
    query_text = " ".join(conn.queries)
    assert "[qc_results].[ACHILLES_ANALYSIS]" in query_text
    assert "[qc_results].[ACHILLES_RESULTS_DIST]" in query_text


def test_missing_core_table_stops_before_summary_sql():
    conn = FakeConnection(missing_analysis=True)
    with pytest.raises(module.AuditBlocked, match="missing Achilles tables"):
        module.reconcile(conn)
    assert len(conn.queries) == 2
    assert conn.closed_cursors == 2


def test_case_collision_stops_before_summary_sql():
    conn = FakeConnection(collision=True)
    with pytest.raises(module.AuditBlocked, match="Ambiguous"):
        module.reconcile(conn)
    assert len(conn.queries) == 2


@pytest.mark.parametrize("values", [
    (259, 77, 335, 2, 294, 294, 194, 154, 0, 0, 0),
    (259, 77, 334, 2, 294, 294, 195, 154, 0, 0, 0),
    (259, 77, 334, 2, 294, 294, 194, 155, 0, 0, 0),
    (259, 77, 334, 2, 294, 295, 194, 154, 0, 0, 0),
    (259, 77, 334, 2, 294, 294, -1, 154, 0, 0, 0),
    (259, 77, 334, 2, 294, 294, 194, 154, 0, 0),
])
def test_invalid_base_set_arithmetic_is_rejected(values):
    with pytest.raises(module.AuditBlocked):
        module.validate_reconciliation(values)


@pytest.mark.parametrize("values", [
    (193, 0, 194, 100, 0, 194, 0, 0, 140, 0, 0),
    (194, 0, 193, 100, 0, 194, 0, 0, 140, 0, 0),
    (194, 0, 194, 100, 0, 193, 0, 0, 140, 0, 0),
    (194, 0, 194, 100, 0, 194, 0, 0, 139, 0, 0),
    (194, 0, 194, 100, 0, 195, 0, 0, 140, 0, 0),
    (194, 0, 194, 100, 0, 194, 0, 0, 140, -1, 0),
    (194, 0, 194, 100, 0, 194, 0, 0, 140, 0),
])
def test_invalid_performance_grouping_is_rejected(values):
    base = module.validate_reconciliation(SUMMARY)
    with pytest.raises(module.AuditBlocked):
        module.validate_performance_namespace(values, base)


def test_reconciliation_excludes_clinical_fields_and_patient_ids():
    code = SCRIPT.read_text(encoding="utf-8")
    sql = code[code.index("def performance_namespace_sql"):code.index("def validate_performance_namespace")]
    assert "count_value" not in sql
    assert "stratum_" not in sql
    assert "patient_id" not in sql
    assert "person_id" not in sql
    assert "EXCEPT SELECT analysis_id FROM definition_ids" in sql
