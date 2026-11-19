"""Synthetic verification tests for the live frozen Achilles 210 reconciliation.

Never accesses institutional databases or their private files.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "verify_postfreeze_achilles_210_live.py"
)
SPEC = importlib.util.spec_from_file_location("achilles_210_live", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _overlap_path(tmp_path, monkeypatch, count):
    folder = tmp_path / "protected"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    path = folder / "analysis_210_locked_d0_overlap.json"
    overlap = None
    if count:
        overlap = {name: 0 for name in module.COLUMNS}
        overlap["source_d0_patients"] = module.EXPECTED_SOURCE_D0
        overlap["omop_lineage_d0_patients"] = module.EXPECTED_OMOP_D0
        overlap["exception_visits"] = count
    payload = {
        "status": "private_postfreeze_achilles_210_cohort_overlap",
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
        "study_definition": "stage-c-stroke-d0-v1",
        "achilles_210_exception_count": count,
        "cohort_sql_performed": bool(count),
        "cohort_overlap": overlap,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(module, "OUTPUT", path)
    return path


@pytest.mark.parametrize("count", [0, 1, 10])
def test_existing_overlap_shape_checks(tmp_path, monkeypatch, count, capsys):
    path = _overlap_path(tmp_path, monkeypatch, count)
    module.verify_previous_overlap_artifact(count)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mutation", [
    lambda p: p.update({"achilles_210_exception_count": 4}),
    lambda p: p.update({"cohort_sql_performed": True}),
    lambda p: p.update({"cohort_overlap": {}}),
    lambda p: p.update({"study_definition": "unlocked"}),
])
def test_zero_overlap_report_must_match_saved_evidence(
    tmp_path, monkeypatch, mutation,
):
    path = _overlap_path(tmp_path, monkeypatch, 0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.AuditBlocked):
        module.verify_previous_overlap_artifact(0)


def test_overlap_report_rejects_symlink_and_excess_permissions(
    tmp_path, monkeypatch,
):
    path = _overlap_path(tmp_path, monkeypatch, 0)
    path.chmod(0o644)
    with pytest.raises(module.AuditBlocked, match="metadata unsafe"):
        module.verify_previous_overlap_artifact(0)
    path.chmod(0o600)
    target = path.with_name("real-result.json")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(module.AuditBlocked):
        module.verify_previous_overlap_artifact(0)


@pytest.mark.parametrize("actual,expected,passes", [
    (0, 0, True), (1, 1, True), (10, 10, True),
    (1, 0, False), (0, 1, False), (11, 10, False),
])
def test_live_query_checks_original_sql_without_disclosing_values(
    monkeypatch, capsys, actual, expected, passes,
):
    called = []
    monkeypatch.setattr(module, "verify_manifest", lambda: called.append("manifest"))
    monkeypatch.setattr(module, "validate_private_metadata", lambda: expected)
    monkeypatch.setattr(module, "verify_previous_overlap_artifact",
                        lambda count: called.append(("artifact", count)))
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic")
    monkeypatch.setattr(module, "verified_server_config", lambda p: {})
    monkeypatch.setattr(module, "make_restricted_odbc_string",
                        lambda cfg, db, password: db)
    monkeypatch.setattr(module, "verify_missing_ids",
                        lambda c: called.append("qc_coverage"))
    monkeypatch.setattr(module, "inspect_frozen",
                        lambda c: called.append("frozen_anchors"))

    def scalar(conn, sql):
        assert sql == module.EXCEPTION_SQL[210]
        assert sql.strip().upper().startswith("SELECT COUNT_BIG(*)")
        called.append("actual_210")
        return actual

    monkeypatch.setattr(module, "single_scalar", scalar)

    class Connection:
        def __init__(self, database):
            self.database = database
        def close(self):
            called.append(("close", self.database))

    class Odbc:
        def connect(self, connection_string, *, autocommit, timeout):
            assert autocommit is True and timeout == 20
            assert connection_string in (module.QC_DATABASE, module.FROZEN_DATABASE)
            return Connection(connection_string)

    monkeypatch.setitem(sys.modules, "pyodbc", Odbc())
    if passes:
        module.confirm_against_frozen("config/etl_A.yaml")
        output = capsys.readouterr().out
        assert "RECONCILIATION: PASS" in output
        assert "exception_count" not in output
        assert "SUPPRESSED_NONZERO" not in output
    else:
        with pytest.raises(module.AuditBlocked, match="does not match"):
            module.confirm_against_frozen("config/etl_A.yaml")
    assert "qc_coverage" in called and "frozen_anchors" in called
    assert "actual_210" in called
    assert ("close", module.QC_DATABASE) in called
    assert ("close", module.FROZEN_DATABASE) in called


def test_missing_restricted_password_blocks_before_any_sql(monkeypatch):
    monkeypatch.setattr(module, "verify_manifest", lambda: None)
    monkeypatch.setattr(module, "validate_private_metadata", lambda: 0)
    monkeypatch.setattr(module, "verify_previous_overlap_artifact", lambda n: None)
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    with pytest.raises(module.AuditBlocked, match="OHDSI_QC_PASSWORD"):
        module.confirm_against_frozen("config/etl_A.yaml")


def test_no_database_or_file_mutation_queries_in_new_script():
    sql = module.EXCEPTION_SQL[210].lower()
    assert sql.lstrip().startswith("select count_big(*)")
    assert "where op.person_id is null" in sql
    source = SCRIPT.read_text(encoding="utf-8").lower()
    for prohibited in ("create table", "insert into", "update [dbo]",
                       "delete from", "drop table", "os.o_creat", "open(\"w\""):
        assert prohibited not in source
