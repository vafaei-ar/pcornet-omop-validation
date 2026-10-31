"""Synthetic tests for SELECT-only Achilles analysis-ID metadata reconciliation.

Uses mock SQL cursors only. No database connection or clinical records.
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


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.type = ""

    def execute(self, sql):
        text = " ".join(sql.strip().split())
        self.conn.queries.append(text)
        assert not any(
            forbidden in text.upper()
            for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE ", "DROP TABLE",
                              "CREATE TABLE", "ALTER TABLE", "TRUNCATE TABLE")
        )
        if "DB_NAME(), ORIGINAL_LOGIN()" in text:
            self.type = "identity"
        elif "FROM sys.tables" in text:
            self.type = "metadata"
        elif "SELECT TOP (20) analysis_id" in text:
            self.type = "sample"
        elif "AS unmatched_results_ids" in text:
            self.type = "summary"
        else:
            raise AssertionError(f"Unexpected query: {text[:100]}")
        return self

    def fetchone(self):
        if self.type == "identity":
            return (self.conn.database, "ohdsi_postfreeze_qc")
        if self.type == "summary":
            return self.conn.values
        return None

    def fetchall(self):
        if self.type == "metadata":
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
        if self.type == "sample":
            return [(2007,), (2010,), (2900,)]
        return []

    def close(self):
        self.conn.closed_cursors += 1


class FakeConnection:
    def __init__(self, values=None, missing_analysis=False, collision=False):
        self.database = "OMOP_VALIDATED_A_postfreeze_qc"
        self.queries = []
        self.closed_cursors = 0
        self.missing_analysis = missing_analysis
        self.collision = collision
        self.values = values or (
            310, 130, 334, 106,
            294, 294, 40, 0,
            0, 0, 0,
        )

    def cursor(self):
        return FakeCursor(self)


def test_reconciliation_computes_orphan_definition_coverage(capsys):
    conn = FakeConnection()
    summary = module.reconcile(conn)
    assert summary["combined_ids"] == 334
    assert summary["definition_rows"] == 294
    assert summary["unmatched_results_ids"] == 40
    assert len(conn.queries) == 4
    assert conn.closed_cursors == len(conn.queries)
    assert all("COUNT_VALUE" not in query.upper() for query in conn.queries)
    assert all("STRATUM_" not in query.upper() for query in conn.queries)
    out = capsys.readouterr().out
    assert "REVIEW REQUIRED" in out
    assert "NOT patient IDs" in out
    assert "2007, 2010, 2900" in out


def test_reconciliation_without_orphans_does_not_query_ids(capsys):
    conn = FakeConnection(values=(294, 130, 294, 130, 294, 294, 0, 0, 0, 0, 0))
    module.reconcile(conn)
    assert len(conn.queries) == 3
    assert "all persisted analysis IDs have definitions" in capsys.readouterr().out


def test_case_mismatched_actual_table_names_are_preserved_in_selects():
    conn = FakeConnection()
    module.reconcile(conn)
    text = " ".join(conn.queries)
    assert "[qc_results].[ACHILLES_ANALYSIS]" in text
    assert "[qc_results].[ACHILLES_RESULTS_DIST]" in text


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
    (310, 130, 335, 106, 294, 294, 40, 0, 0, 0, 0),
    (310, 130, 334, 106, 294, 294, 41, 0, 0, 0, 0),
    (310, 130, 334, 106, 294, 294, 40, 1, 0, 0, 0),
    (310, 130, 334, 106, 294, 295, 40, 0, 0, 0, 0),
    (310, 130, 334, 106, 294, 294, -1, 0, 0, 0, 0),
    (310, 130, 334, 106, 294, 294, 40, 0, 0, 0),
])
def test_invalid_set_arithmetic_is_rejected(values):
    with pytest.raises(module.AuditBlocked):
        module.validate_reconciliation(values)


def test_reconciliation_does_not_expose_results_or_clinical_fields():
    code = SCRIPT.read_text(encoding="utf-8")
    sql = code[code.index("def reconciliation_sql"):code.index("def unmatched_id_sample_sql")]
    assert "count_value" not in sql
    assert "stratum_" not in sql
    assert "patient_id" not in sql
    assert "person_id" not in sql
    assert "EXCEPT SELECT analysis_id FROM definition_ids" in sql
