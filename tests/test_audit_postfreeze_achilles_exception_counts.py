"""Synthetic checks for guarded four-ID Achilles exception-count triage.

No institutional SQL, patient data, or stored Achilles counts are accessed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts"
          / "audit_postfreeze_achilles_exception_counts.py")
SPEC = importlib.util.spec_from_file_location("achilles_exception_counts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_official_count_predicates_are_exact_and_select_only():
    queries = module.EXCEPTION_SQL
    assert set(queries) == {207, 209, 210, 814}
    for query in queries.values():
        normalized = " ".join(query.lower().split())
        assert normalized.startswith("select count_big(")
        for forbidden in ("insert ", "update ", "delete ", "drop ",
                          "create ", "alter ", "select *", "into ["):
            assert forbidden not in normalized
        assert " from [dbo]." in normalized
    assert "count_big(vo1.person_id)" in queries[207].lower()
    assert "p1.person_id is null" in queries[207].lower()
    assert "vo1.care_site_id is not null" in queries[209].lower()
    assert "cs1.care_site_id is null" in queries[209].lower()
    assert "count_big(*)" in queries[210].lower()
    assert "vo.visit_start_date >= op.observation_period_start_date" in queries[210].lower()
    assert "vo.visit_start_date <= op.observation_period_end_date" in queries[210].lower()
    assert "op.person_id is null" in queries[210].lower()
    for name in ("value_as_number", "value_as_string", "value_as_concept_id"):
        assert f"o1.{name} is null" in queries[814].lower()


class CoverageCursor:
    def __init__(self):
        self.queries = []
        self.closed = False

    def execute(self, sql):
        self.queries.append(sql)
        assert sql.lstrip().startswith(";WITH stored_ids AS")

    def fetchall(self):
        return ["synthetic_coverage"]

    def close(self):
        self.closed = True


class CoverageConn:
    def __init__(self):
        self.c = CoverageCursor()

    def cursor(self):
        return self.c


def _definitions(overrides=None):
    records = [
        {"id": analysis_id, "default": 1, "category": category,
         "has_data": 0, "has_performance": 1}
        for analysis_id, category in module.CATEGORIES.items()
    ]
    for id_, updates in (overrides or {}).items():
        next(record for record in records if record["id"] == id_).update(updates)
    return records


def test_missing_id_gate_accepts_only_expected_saved_status(monkeypatch):
    conn = CoverageConn()
    monkeypatch.setattr(module, "verify_identity", lambda c, d: None)
    monkeypatch.setattr(module, "discover_achilles_tables", lambda c: {})
    monkeypatch.setattr(module, "coverage_sql", lambda tables: ";WITH stored_ids AS (SELECT 1)")
    monkeypatch.setattr(module, "parse_definition_rows", lambda rows: _definitions())
    calls = []
    monkeypatch.setattr(module, "summarize_coverage",
                        lambda rows: calls.append(tuple(rows)))
    module.verify_missing_ids(conn)
    assert conn.c.closed
    assert len(conn.c.queries) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("override", [
    {"has_data": 1}, {"has_performance": 0},
    {"default": 0}, {"category": "Visit Occurrence"},
])
def test_missing_id_gate_blocks_coverage_changes(monkeypatch, override):
    monkeypatch.setattr(module, "verify_identity", lambda c, d: None)
    monkeypatch.setattr(module, "discover_achilles_tables", lambda c: {})
    monkeypatch.setattr(module, "coverage_sql", lambda tables: ";WITH stored_ids AS (SELECT 1)")
    monkeypatch.setattr(module, "parse_definition_rows",
                        lambda rows: _definitions({210: override}))
    monkeypatch.setattr(module, "summarize_coverage", lambda rows: None)
    with pytest.raises(module.AuditBlocked, match="status has changed"):
        module.verify_missing_ids(CoverageConn())


def test_counts_are_read_only_and_no_values_are_printed(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(module, "verify_identity", lambda *args: None)

    def scalar(conn, sql):
        seen.append(sql)
        return [0, 1, 10, 0][len(seen) - 1]

    monkeypatch.setattr(module, "single_scalar", scalar)
    counts = module.read_exception_counts(object())
    assert counts == {207: 0, 209: 1, 210: 10, 814: 0}
    assert len(seen) == 4
    assert all(s.lstrip().upper().startswith("SELECT COUNT_BIG(") for s in seen)
    assert capsys.readouterr().out == ""


def test_count_above_suppression_limit_blocks(monkeypatch):
    monkeypatch.setattr(module, "verify_identity", lambda *args: None)
    monkeypatch.setattr(module, "single_scalar", lambda c, q: 11)
    with pytest.raises(module.AuditBlocked, match="exceeds saved suppression"):
        module.read_exception_counts(object())


def _private_test_path(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    folder = tmp_path / "results" / "postfreeze_standard_qc" / "private_achilles_exception_counts"
    monkeypatch.setattr(module, "PRIVATE_DIR", folder)
    monkeypatch.setattr(module, "PRIVATE_OUTPUT", folder / "four_analysis_exact_counts.json")
    return folder / "four_analysis_exact_counts.json"


def test_private_result_is_0600_and_not_overwritten(tmp_path, monkeypatch):
    path = _private_test_path(tmp_path, monkeypatch)
    module.save_private_counts({207: 0, 209: 2, 210: 10, 814: 0}, path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["analyses"]["210"]["exception_count"] == 10
    assert result["analyses"]["814"]["classification"] == "ZERO"
    assert result["analyses"]["209"]["classification"] == "SUPPRESSED_NONZERO"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(module.AuditBlocked, match="refusing overwrite"):
        module.save_private_counts({207: 1, 209: 1, 210: 1, 814: 1}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == result


def test_private_counts_reject_out_of_range_and_missing_ids(tmp_path, monkeypatch):
    path = _private_test_path(tmp_path, monkeypatch)
    for invalid in (
        {207: 0, 209: 0, 210: 0},
        {207: 0, 209: 0, 210: 11, 814: 0},
        {207: 0, 209: 0, 210: -1, 814: 0},
        {207: 0, 209: 0, 210: True, 814: 0},
    ):
        with pytest.raises(module.AuditBlocked):
            module.save_private_counts(invalid, path)
    assert not path.exists()


def test_private_directory_refuses_symlink_and_insecure_permissions(
    tmp_path, monkeypatch,
):
    path = _private_test_path(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o755)
    with pytest.raises(module.AuditBlocked, match="mode 0700"):
        module.save_private_counts({207: 0, 209: 0, 210: 0, 814: 0}, path)
    path.parent.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(module.AuditBlocked, match="symlink"):
        module.save_private_counts({207: 0, 209: 0, 210: 0, 814: 0}, path)


def test_diagnostic_never_exposes_exact_counts_on_stdout(tmp_path, monkeypatch, capsys):
    # Isolate the path checked by inspect() itself, even when the mocked save
    # function never writes. Never depend on institutional results/ files.
    _private_test_path(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "verify_manifest", lambda: None)
    monkeypatch.setattr(module, "verified_server_config", lambda p: {"server": "localhost"})
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "private_dummy")
    monkeypatch.setattr(module, "verify_missing_ids", lambda c: None)
    monkeypatch.setattr(module, "inspect_frozen", lambda c: None)
    monkeypatch.setattr(module, "read_exception_counts",
                        lambda c: {207: 2, 209: 0, 210: 10, 814: 1})
    saved = []
    monkeypatch.setattr(module, "save_private_counts", lambda counts: saved.append(counts))
    monkeypatch.setattr(module, "make_restricted_odbc_string",
                        lambda c, d, p: "synthetic")

    class Conn:
        def close(self):
            pass

    class PyOdbc:
        def connect(self, *a, **kw):
            assert kw["autocommit"] is True
            return Conn()

    monkeypatch.setitem(__import__("sys").modules, "pyodbc", PyOdbc())
    module.inspect("config/etl_A.yaml")
    stdout = capsys.readouterr().out
    assert "READ-ONLY TRIAGE: PASS" in stdout
    assert "DO NOT upload" in stdout
    assert "exception_count" not in stdout
    assert saved == [{207: 2, 209: 0, 210: 10, 814: 1}]



def test_verify_existing_file_has_no_db_access_or_count_disclosure(
    tmp_path, monkeypatch, capsys,
):
    path = _private_test_path(tmp_path, monkeypatch)
    module.save_private_counts({207: 0, 209: 2, 210: 10, 814: 0}, path)

    def refuse_database_access(*args, **kwargs):
        raise AssertionError("Existing-output verification must not access database")

    monkeypatch.setattr(module, "verified_server_config", refuse_database_access)
    monkeypatch.setattr(module, "read_exception_counts", refuse_database_access)
    module.verify_existing_private_output()
    output = capsys.readouterr().out
    assert "STRUCTURE/PERMISSIONS PASS" in output
    assert "no SQL connection" in output
    assert "exception_count" not in output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mutation", [
    lambda d: d["analyses"]["210"].update({"classification": "ZERO"}),
    lambda d: d["analyses"]["210"].update({"exception_count": 11}),
    lambda d: d["analyses"].pop("814"),
    lambda d: d.update({"small_cell_threshold": 0}),
    lambda d: d.update({"recorded_at_utc": "2026-10-31"}),
])
def test_verify_existing_rejects_tampering(tmp_path, monkeypatch, mutation, capsys):
    path = _private_test_path(tmp_path, monkeypatch)
    module.save_private_counts({207: 0, 209: 2, 210: 10, 814: 0}, path)
    document = json.loads(path.read_text(encoding="utf-8"))
    mutation(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(module.AuditBlocked):
        module.verify_existing_private_output()
    assert "PASS" not in capsys.readouterr().out


def test_verify_existing_rejects_broad_perms_and_symlink(
    tmp_path, monkeypatch,
):
    path = _private_test_path(tmp_path, monkeypatch)
    module.save_private_counts({207: 0, 209: 2, 210: 10, 814: 0}, path)
    path.chmod(0o644)
    with pytest.raises(module.AuditBlocked, match="permissions"):
        module.verify_existing_private_output()
    path.chmod(0o600)
    target = path.with_name("protected-original")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(module.AuditBlocked):
        module.verify_existing_private_output()


def test_verify_existing_rejects_missing_file_without_creating_directory(
    tmp_path, monkeypatch,
):
    path = _private_test_path(tmp_path, monkeypatch)
    with pytest.raises(module.AuditBlocked):
        module.verify_existing_private_output()
    assert not path.parent.exists()
