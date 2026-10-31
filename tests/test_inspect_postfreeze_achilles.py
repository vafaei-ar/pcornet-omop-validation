"""Synthetic tests for the local, SELECT-only Achilles post-run audit.

The test suite does not read real Achilles result values or connect to SQL.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_postfreeze_achilles.py"
SPEC = importlib.util.spec_from_file_location("inspect_postfreeze_achilles", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def fixture_manifest(tmp_path: Path, **overrides):
    data = {
        "status": "postfreeze_standard_qc_execution_complete",
        "analysis_role": "exploratory_standard_OMOP_target_only_QC",
        "tool": "achilles",
        "tool_version": "1.7.2",
        "target_cdm_version": "5.4.2",
        "frozen_etl_sha": audit.FROZEN_ETL_SHA,
        "target_cdm_counts_preserved": True,
        "output_local_only": True,
        "disclosure_review_required_before_publication": True,
        "frozen_cdm_source_row_count": 0,
        "frozen_cdm_source_populated": False,
        "cdm_source_metadata_gap_unresolved": True,
        "achilles_small_cell_threshold": 10,
        "recorded_at_utc": "2026-10-30 21:55:00 UTC",
    }
    data.update(overrides)
    path = tmp_path / "execution_manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_manifest_requires_true_success_and_unresolved_cdm_source(tmp_path):
    path = fixture_manifest(tmp_path)
    assert audit.verify_manifest(path)["tool_version"] == "1.7.2"
    path = fixture_manifest(tmp_path, cdm_source_metadata_gap_unresolved=False)
    with pytest.raises(audit.AuditBlocked, match="missing required safety flag"):
        audit.verify_manifest(path)
    path = fixture_manifest(tmp_path, frozen_cdm_source_row_count=1)
    with pytest.raises(audit.AuditBlocked, match="empty frozen CDM_SOURCE"):
        audit.verify_manifest(path)


def test_manifest_rejects_wrong_tool_or_disclosure_flags(tmp_path):
    with pytest.raises(audit.AuditBlocked, match="manifest mismatch: tool"):
        audit.verify_manifest(fixture_manifest(tmp_path, tool="dqd"))
    with pytest.raises(audit.AuditBlocked, match="missing required safety flag"):
        audit.verify_manifest(fixture_manifest(tmp_path, output_local_only=False))
    with pytest.raises(audit.AuditBlocked, match="smallCellCount=10"):
        audit.verify_manifest(fixture_manifest(tmp_path, achilles_small_cell_threshold=0))


def test_missing_or_symlinked_manifest_is_blocked(tmp_path):
    with pytest.raises(audit.AuditBlocked, match="missing or unsafe"):
        audit.verify_manifest(tmp_path / "not_created.json")
    original = fixture_manifest(tmp_path)
    link = tmp_path / "shortcut.json"
    link.symlink_to(original)
    with pytest.raises(audit.AuditBlocked, match="missing or unsafe"):
        audit.verify_manifest(link)


def test_odbc_connection_only_uses_restricted_login_and_escapes_password():
    cfg = {"server": "localhost", "driver": "ODBC Driver 18 for SQL Server", "trust": "yes"}
    value = audit.make_restricted_odbc_string(
        cfg, audit.QC_DATABASE, "test;with}a}semicolon"
    )
    assert "UID=ohdsi_postfreeze_qc;" in value
    assert "DATABASE=OMOP_VALIDATED_A_postfreeze_qc;" in value
    assert "PWD={test;with}}a}}semicolon}" in value
    assert "Encrypt=yes;TrustServerCertificate=yes;" in value
    with pytest.raises(audit.AuditBlocked, match="Unexpected database"):
        audit.make_restricted_odbc_string(cfg, "master", "secret")
    with pytest.raises(audit.AuditBlocked, match="missing"):
        audit.make_restricted_odbc_string(cfg, audit.QC_DATABASE, "")


def test_config_rejects_non_frozen_target(tmp_path):
    def config(target):
        path = tmp_path / "etl.yaml"
        path.write_text(
            yaml.safe_dump({
                "etl": {"cdm_version": "5.4.2", "backend": "sqlserver"},
                "source": {"parquet_dir": "/nonexistent"},
                "sqlserver": {
                    "server": "localhost", "database": target,
                    "user": "SA", "password_env": "OMOP_SQL_PASSWORD",
                    "target_schema": "dbo",
                    "driver": "ODBC Driver 18 for SQL Server",
                    "trust_server_certificate": True,
                },
                "vocabulary": {"directory": "/nonexistent"},
                "output": {"parquet_dir": "/nonexistent"},
            }), encoding="utf-8",
        )
        return str(path)

    with pytest.raises(audit.AuditBlocked, match="unexpected frozen database"):
        audit.verified_server_config(config("OTHER_DB"))
    result = audit.verified_server_config(config("OMOP_VALIDATED_A"))
    assert result["trust"] == "yes"
    assert result["driver"] == "ODBC Driver 18 for SQL Server"


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.value = None
        self.rows = None

    def execute(self, statement):
        normalized = " ".join(statement.split())
        self.connection.statements.append(normalized)
        assert normalized.startswith("SELECT "), "The audit must issue SELECT only"
        if "DB_NAME(), ORIGINAL_LOGIN()" in normalized:
            self.value = (
                self.connection.database, "ohdsi_postfreeze_qc"
            )
        elif "FROM sys.tables" in normalized:
            self.rows = [
                ("qc_results", "achilles_analysis"),
                ("qc_results", "achilles_results"),
                ("qc_results", "achilles_results_dist"),
            ]
            if self.connection.with_scratch:
                self.rows.append(("qc_scratch", "tmpach_dummy"))
            if self.connection.missing_results:
                self.rows.remove(("qc_results", "achilles_results_dist"))
        elif "UNION" in normalized:
            self.value = (150,)
        elif "[qc_results].[achilles_analysis]" in normalized:
            self.value = (194,)
        elif "[qc_results].[achilles_results_dist]" in normalized:
            self.value = (7000,)
        elif "[qc_results].[achilles_results]" in normalized:
            self.value = (20000,)
        elif "[dbo].[person]" in normalized:
            self.value = (27089,)
        elif "[dbo].[visit_occurrence]" in normalized:
            self.value = (1510957,)
        elif "[dbo].[condition_occurrence]" in normalized:
            self.value = (7315572,)
        elif "[dbo].[cdm_source]" in normalized:
            self.value = (0,)
        else:
            raise AssertionError("Unexpected query in synthetic SQL audit")
        return self

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.rows

    def close(self):
        self.connection.closed_cursors += 1


class FakeConn:
    def __init__(self, database, missing_results=False, with_scratch=False):
        self.database = database
        self.statements = []
        self.closed_cursors = 0
        self.missing_results = missing_results
        self.with_scratch = with_scratch

    def cursor(self):
        return FakeCursor(self)


def test_reads_only_metadata_and_result_row_counts(capsys):
    qc = FakeConn(audit.QC_DATABASE)
    totals = audit.inspect_results(qc)
    assert totals["achilles_analysis"] == 194
    assert len(qc.statements) == 6
    assert qc.closed_cursors == len(qc.statements)
    assert all(query.startswith("SELECT ") for query in qc.statements)
    out = capsys.readouterr().out
    assert "not patient counts" in out
    assert "Distinct analysis IDs represented" in out

    frozen = FakeConn(audit.FROZEN_DATABASE)
    audit.inspect_frozen(frozen)
    assert len(frozen.statements) == 5
    assert all(query.startswith("SELECT ") for query in frozen.statements)
    assert "0 rows, unresolved provenance" in capsys.readouterr().out


def test_missing_core_tables_is_not_reported_as_success():
    qc = FakeConn(audit.QC_DATABASE, missing_results=True)
    with pytest.raises(audit.AuditBlocked, match="Missing core Achilles"):
        audit.inspect_results(qc)


def test_remaining_scratch_tables_are_reported_without_mutation(capsys):
    qc = FakeConn(audit.QC_DATABASE, with_scratch=True)
    audit.inspect_results(qc)
    assert "WARNING: inspect remaining scratch tables" in capsys.readouterr().out


def test_helper_rejects_non_select_sql_without_execution():
    qc = FakeConn(audit.QC_DATABASE)
    with pytest.raises(audit.AuditBlocked, match="Only SELECT"):
        audit.single_scalar(qc, "DELETE FROM [dbo].[person]")
    assert qc.statements == []
