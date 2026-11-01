"""Synthetic, no-clinical-data tests for Achilles category coverage inventory."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_postfreeze_achilles_coverage.py"
SPEC = importlib.util.spec_from_file_location("achilles_category_coverage", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def synthetic_definitions():
    default = [
        (i, 1, ("Person" if i % 2 == 0 else "Drug Exposure"),
         1 if i < 140 else 0, 1)
        for i in range(194)
    ]
    non_default = [(1000 + i, 0, "General", 0, 0) for i in range(100)]
    return default + non_default


class FakeCursor:
    def __init__(self, connection):
        self.conn = connection
        self.kind = ""

    def execute(self, sql):
        query = " ".join(sql.split())
        self.conn.queries.append(query)
        assert "count_value" not in query.lower()
        assert "stratum_" not in query.lower()
        assert not any(
            pattern in query.upper()
            for pattern in ("INSERT INTO", "DELETE FROM", "UPDATE ",
                            "CREATE TABLE", "DROP TABLE",
                            "ALTER TABLE", "TRUNCATE TABLE")
        )
        if "DB_NAME(), ORIGINAL_LOGIN()" in query:
            self.kind = "identity"
        elif "FROM sys.tables" in query:
            self.kind = "tables"
        elif "AS has_performance_row" in query:
            self.kind = "coverage"
        else:
            raise AssertionError("Unexpected SQL in category coverage audit")
        return self

    def fetchone(self):
        if self.kind == "identity":
            return self.conn.database, "ohdsi_postfreeze_qc"
        raise AssertionError("Unexpected single-row query")

    def fetchall(self):
        if self.kind == "tables":
            return [
                ("qc_results", "ACHILLES_ANALYSIS"),
                ("qc_results", "achilles_results"),
                ("qc_results", "ACHILLES_RESULTS_DIST"),
            ]
        if self.kind == "coverage":
            return self.conn.rows
        raise AssertionError("Unexpected multi-row query")

    def close(self):
        self.conn.closes += 1


class FakeConn:
    database = "OMOP_VALIDATED_A_postfreeze_qc"

    def __init__(self, rows=None):
        self.queries = []
        self.closes = 0
        self.rows = synthetic_definitions() if rows is None else rows

    def cursor(self):
        return FakeCursor(self)


def test_coverage_by_category_uses_only_metadata_and_id_presence(capsys):
    conn = FakeConn()
    coverage = module.inspect(conn)
    assert coverage["defaults_with_data"] == 140
    assert coverage["defaults_without_data"] == 54
    assert coverage["defaults_with_performance"] == 194
    assert coverage["nondefaults_with_data"] == 0
    assert coverage["nondefaults_with_performance"] == 0
    assert len(conn.queries) == 3
    assert conn.closes == len(conn.queries)
    assert all("COUNT_VALUE" not in q.upper() for q in conn.queries)
    assert all("STRATUM_" not in q.upper() for q in conn.queries)
    out = capsys.readouterr().out
    assert "CATEGORY COVERAGE AUDIT: PASS" in out
    assert "54" in out
    assert "NOT persons or source records" in out
    assert "Default analysis IDs without data-bearing rows" not in out


def test_missing_analysis_ids_are_optional_and_not_patient_ids(capsys):
    conn = FakeConn()
    module.inspect(conn, show_ids=True)
    output = capsys.readouterr().out
    assert "Default analysis IDs without data-bearing rows (NOT patient IDs)" in output
    assert "190" in output
    assert "Person" in output
    assert "Drug Exposure" in output


def test_exact_upstream_performance_offset_is_used_in_sql():
    tables = {
        "achilles_analysis": "[qc_results].[ACHILLES_ANALYSIS]",
        "achilles_results": "[qc_results].[achilles_results]",
        "achilles_results_dist": "[qc_results].[ACHILLES_RESULTS_DIST]",
    }
    query = module.coverage_sql(tables)
    assert "data_id.analysis_id = a.analysis_id" in query
    assert "a.analysis_id + 2000000" in query
    assert "count_value" not in query
    assert "stratum_" not in query
    assert "person_id" not in query


def test_expected_definition_and_default_counts_are_checked():
    records = module.parse_definition_rows(synthetic_definitions())
    assert len(records) == 294
    assert sum(record["default"] for record in records) == 194
    with pytest.raises(module.AuditBlocked, match="definition count"):
        module.parse_definition_rows(synthetic_definitions()[:293])


def test_duplicate_ids_are_blocked():
    rows = synthetic_definitions()
    rows[-1] = (0, 0, "General", 0, 0)
    with pytest.raises(module.AuditBlocked, match="Duplicated"):
        module.parse_definition_rows(rows)


def test_invalid_category_or_flags_are_blocked():
    for modified in (
        (0, 1, "Clinical\nInjection", 1, 1),
        (0, 1, "", 1, 1),
        (0, 1, "Person", 2, 1),
        (0, 1, "Person", 1, 2),
        (0, 2, "Person", 1, 1),
        (0, 1, "Person", 1, None),
        (2_000_000, 1, "Person", 1, 1),
    ):
        rows = synthetic_definitions()
        rows[0] = modified
        with pytest.raises(module.AuditBlocked):
            module.parse_definition_rows(rows)


def test_frozen_snapshot_coverage_drift_is_not_misreported_as_success():
    rows = synthetic_definitions()
    rows[0] = (0, 1, "Person", 0, 1)
    records = module.parse_definition_rows(rows)
    with pytest.raises(module.AuditBlocked, match="differs from"):
        module.summarize_coverage(records)


def test_non_default_data_is_not_counted_as_a_default_success():
    rows = synthetic_definitions()
    rows[200] = (1006, 0, "General", 1, 0)
    records = module.parse_definition_rows(rows)
    with pytest.raises(module.AuditBlocked, match="differs from"):
        module.summarize_coverage(records)


def test_wrong_database_identity_is_blocked_before_any_result_query():
    conn = FakeConn()
    conn.database = "master"
    with pytest.raises(module.AuditBlocked, match="restricted login identity"):
        module.inspect(conn)
    assert len(conn.queries) == 1


def test_result_categories_have_no_sensitive_values():
    source = SCRIPT.read_text(encoding="utf-8")
    sql = source[source.index("def coverage_sql"):source.index("def parse_definition_rows")]
    for forbidden in ("count_value", "stratum_", "person_id",
                      "concept_id", "source_value", "observation_period_id"):
        assert forbidden not in sql.lower()
    assert "CASE WHEN" in sql
    assert "SELECT" in sql
