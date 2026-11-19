"""Synthetic tests for restricted read-only CDM table-presence triage.

No SQL connections and no clinical values. These tests check only the
interpretability of existing Achilles category coverage and binary row
existence, not counts or mapping correctness.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "triage_postfreeze_achilles_source_tables.py"
SPEC = importlib.util.spec_from_file_location("omop_source_presence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


# Category counts from the prior local, verified Achilles output.
# Synthetic fixtures: never patient counts or source-table row counts.
NO_DATA = {
    "Visit Detail": (12, 1, 11),
    "Payer Plan Period": (9, 0, 9),
    "Condition Era": (8, 0, 8),
    "Drug Era": (8, 0, 8),
    "Care Site": (4, 0, 4),
    "Location": (4, 0, 4),
    "Provider": (4, 0, 4),
    "Note": (2, 0, 2),
    "Visit Occurrence": (14, 12, 2),
    "Observation": (17, 16, 1),
    "Observation Period": (16, 15, 1),
}


def coverage():
    return {
        "defaults_with_data": 140,
        "defaults_without_data": 54,
        "categories": {
            category: {"defined": values[0],
                       "has_data": values[1],
                       "no_data": values[2]}
            for category, values in NO_DATA.items()
        },
    }


def presence(**overrides):
    values = {name: False for name in module.ALLOWLIST}
    values.update({
        "condition_occurrence": True, "drug_exposure": True, "person": True,
        "visit_occurrence": True, "observation": True,
        "observation_period": True,
    })
    values.update(overrides)
    return values


def test_all_54_missing_result_analyses_are_accounted_for():
    rows = module.categorize(coverage(), presence())
    assert sum(item["without_data"] for item in rows) == 54
    assert rows[0]["category"] == "Visit Detail"
    assert rows[0]["without_data"] == 11
    assert rows[1]["category"] == "Payer Plan Period"
    assert len(rows) == 11
    assert set(module.CATEGORY_TO_CDM_TABLE) == set(NO_DATA)


def test_frozen_era_absence_is_described_as_hypothesis_not_failure(capsys):
    rows = module.categorize(coverage(), presence())
    module.present_report(rows, presence())
    output = capsys.readouterr().out
    assert "condition_era has no rows despite a populated condition_occurrence" in output
    assert "drug_era has no rows despite a populated drug_exposure" in output
    assert "Check whether the approved ETL includes era construction" in output
    assert "a quality-check failure" not in output


def test_nonempty_source_without_achilles_rows_gets_review(capsys):
    present = presence(condition_era=True, drug_era=True, visit_detail=True)
    rows = module.categorize(coverage(), present)
    module.present_report(rows, present)
    output = capsys.readouterr().out
    assert "condition_era contains rows; missing Achilles results need" in output
    assert "drug_era contains rows; missing Achilles results need" in output
    assert "one or more nonempty OMOP tables have absent Achilles results" in output
    assert "SOURCE-TABLE TRIAGE: PASS" in output


def test_presence_query_is_constant_only_and_preserves_actual_identifier(monkeypatch):
    seen = []

    def fake_scalar(connection, statement):
        seen.append(statement)
        return 1

    monkeypatch.setattr(module, "single_scalar", fake_scalar)
    assert module.has_any_row(object(), "condition_era", "CONDITION_ERA") is True
    assert seen == [
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM [dbo].[CONDITION_ERA]) "
        "THEN 1 ELSE 0 END"
    ]


def test_presence_query_rejects_unsanctioned_tables_and_bad_case_mapping(
    monkeypatch,
):
    seen = []
    monkeypatch.setattr(module, "single_scalar",
                        lambda conn, sql: seen.append(sql) or 1)
    with pytest.raises(module.AuditBlocked, match="allowlist"):
        module.has_any_row(object(), "patients", "patients")
    with pytest.raises(module.AuditBlocked, match="allowlist"):
        module.has_any_row(object(), "condition_era", "person")
    assert seen == []


def test_presence_query_rejects_unexpected_nonbinary_results(monkeypatch):
    monkeypatch.setattr(module, "single_scalar", lambda conn, sql: 2)
    with pytest.raises(module.AuditBlocked, match="presence result"):
        module.has_any_row(object(), "note", "note")


def test_missing_expected_cdm_table_blocks_diagnosis(monkeypatch):
    monkeypatch.setattr(module, "verify_identity", lambda *args: None)

    class Cursor:
        def execute(self, sql):
            assert "FROM sys.tables" in sql
        def fetchall(self):
            return [(name,) for name in module.ALLOWLIST if name != "drug_era"]
        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(module.AuditBlocked, match="drug_era"):
        module.actual_frozen_tables(Connection())


def test_actual_metadata_preserves_uppercase_table_names(monkeypatch):
    monkeypatch.setattr(module, "verify_identity", lambda *args: None)

    class Cursor:
        def execute(self, sql):
            assert "FROM sys.tables" in sql
        def fetchall(self):
            return [(name.upper(),) for name in module.ALLOWLIST]
        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    actual = module.actual_frozen_tables(Connection())
    assert actual["condition_era"] == "CONDITION_ERA"


def test_strict_source_presence_and_coverage_totals():
    status = presence()
    status.pop("person")
    with pytest.raises(module.AuditBlocked, match="presence evidence"):
        module.categorize(coverage(), status)
    status = presence()
    status["person"] = 1
    with pytest.raises(module.AuditBlocked, match="presence evidence"):
        module.categorize(coverage(), status)
    c = coverage()
    c["defaults_without_data"] = 53
    with pytest.raises(module.AuditBlocked, match="totals"):
        module.categorize(c, presence())


def test_only_binary_exists_and_metadata_queries_in_script():
    code = SCRIPT.read_text(encoding="utf-8")
    assert "EXISTS (SELECT 1 FROM [dbo]." in code
    assert "make_restricted_odbc_string(cfg, QC_DATABASE, secret)" in code
    assert "make_restricted_odbc_string(cfg, FROZEN_DATABASE, secret)" in code
    assert "inspect_frozen(frozen)" in code
    assert "COUNT_VALUE" not in code.upper()
    assert "STRATUM_1" not in code.upper()
    assert "SELECT *" not in code.upper()
    assert "UPDATE [dbo]" not in code.upper()
    assert "INSERT INTO [dbo]" not in code.upper()
