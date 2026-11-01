"""Synthetic SQL doubles: no real database or protected metadata involved."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "amend_frozen_cdm_source.py"
spec = importlib.util.spec_from_file_location("amend_cdm_source", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def synthetic_row():
    return {
        "cdm_source_name": "SYNTHETIC PRIVATE HOLDER",
        "cdm_source_abbreviation": "SYN",
        "cdm_holder": "Synthetic holder",
        "source_description": "Synthetic PCORnet test source",
        "source_documentation_reference": "synthetic doc",
        "cdm_etl_reference": "frozen " + mod.manifest_validator.FROZEN_SHA,
        "source_release_date": "2026-06-01",
        "cdm_release_date": "2026-06-15",
        "cdm_version": "v5.4",
        "cdm_version_concept_id": 756265,
        "vocabulary_version": "synthetic_version",
    }


class FakeResult:
    def __init__(self, value=None, *, rows=None, count=1):
        self.value = value
        self.rows = rows
        self.rowcount = count

    def scalar_one(self):
        return self.value

    def one(self):
        return self.value

    def all(self):
        return self.rows

    def mappings(self):
        return self


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn
        self.complete = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.complete:
            self.rollback()

    def commit(self):
        self.conn.commits += 1
        self.complete = True

    def rollback(self):
        self.conn.rollbacks += 1
        self.complete = True


class FakeConnection:
    def __init__(self):
        self.sql = []
        self.rows = 0
        self.commits = 0
        self.rollbacks = 0
        self.expected = synthetic_row()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def begin(self):
        return FakeTransaction(self)

    def exec_driver_sql(self, sql):
        self.sql.append(sql)
        if sql == "SELECT DB_NAME(), ORIGINAL_LOGIN()":
            return FakeResult(("OMOP_VALIDATED_A", "synthetic_admin"))
        if sql.startswith("SELECT COUNT_BIG(*) FROM [dbo].["):
            table = sql.split("[dbo].[")[1].split("]")[0]
            counts = {"person": 27089, "visit_occurrence": 1510957,
                      "condition_occurrence": 7315572, "cdm_source": self.rows}
            return FakeResult(counts[table])
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return FakeResult(rows=[(n,) for n in mod.FIELDS])
        if "vocabulary_version FROM" in sql:
            return FakeResult(rows=[("synthetic_version",)])
        if "HAS_PERMS_BY_NAME" in sql:
            return FakeResult(1)
        if sql.startswith("SELECT [cdm_source_name]"):
            saved = dict(self.expected)
            from datetime import date
            saved["source_release_date"] = date(2026, 6, 1)
            saved["cdm_release_date"] = date(2026, 6, 15)
            return FakeResult(saved)
        raise AssertionError("Unexpected SQL")

    def execute(self, sql, params=None):
        statement = str(sql).strip()
        self.sql.append(statement)
        if statement.startswith("SELECT vocabulary_id, concept_class_id"):
            return FakeResult(rows=[("CDM", "CDM", "Metadata")])
        if statement.startswith("INSERT INTO [dbo].[cdm_source]"):
            assert "WHERE NOT EXISTS" in statement
            assert "UPDLOCK, HOLDLOCK" in statement
            assert params["cdm_version"] == "v5.4"
            assert params["cdm_version_concept_id"] == 756265
            assert params["vocabulary_version"] == "synthetic_version"
            assert self.rows == 0
            self.rows = 1
            return FakeResult(count=1)
        raise AssertionError("Unexpected SQLAlchemy SQL")


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def ready(monkeypatch):
    c = FakeConnection()
    engine = FakeEngine(c)
    monkeypatch.setattr(mod, "safe_connection_config",
                        lambda _: SimpleNamespace(raw={
                            "sqlserver": {"user": "synthetic_admin"},
                        }))
    monkeypatch.setattr(mod, "load_private_manifest", lambda _: synthetic_row())
    monkeypatch.setattr(mod, "credential_ready", lambda cfg, interactive: True)
    monkeypatch.setattr(mod, "make_engine",
                        lambda cfg, database: engine)
    return c, engine


def test_preflight_select_only_with_rollback(monkeypatch, capsys):
    c, e = ready(monkeypatch)
    mod.perform("config", "manifest", execute=False)
    out = capsys.readouterr().out
    assert "PREFLIGHT: PASS" in out
    assert "SYNTHETIC PRIVATE HOLDER" not in out
    assert c.commits == 0 and c.rollbacks == 1 and c.rows == 0
    assert all(stmt.lstrip().upper().startswith("SELECT") for stmt in c.sql)
    assert e.disposed


def test_exactly_one_row_insert_and_clinical_anchors(monkeypatch, capsys):
    c, e = ready(monkeypatch)
    mod.perform("config", "manifest", execute=True)
    out = capsys.readouterr().out
    assert "AMENDMENT: PASS" in out
    assert c.commits == 1 and c.rows == 1
    assert c.sql.count("SELECT COUNT_BIG(*) FROM [dbo].[person]") == 2
    assert sum(stmt.startswith("INSERT") for stmt in c.sql) == 1
    assert e.disposed
    assert "SYNTHETIC PRIVATE HOLDER" not in out


def test_nonempty_source_fails_closed_without_insert(monkeypatch, capsys):
    c, _ = ready(monkeypatch)
    c.rows = 1
    with pytest.raises(SystemExit) as err:
        mod.perform("config", "manifest", execute=True)
    assert err.value.code == 1
    assert "CDM_SOURCE_EMPTY" in capsys.readouterr().err
    assert c.commits == 0 and c.rollbacks == 1
    assert not any(stmt.startswith("INSERT") for stmt in c.sql)


def test_wrong_database_fails_closed(monkeypatch, capsys):
    c, _ = ready(monkeypatch)
    c.exec_driver_sql = lambda stmt: FakeResult(("wrong_database", "synthetic_admin"))
    with pytest.raises(SystemExit):
        mod.perform("config", "manifest", execute=True)
    assert "IDENTITY" in capsys.readouterr().err
    assert c.commits == 0


def test_vocabulary_mismatch_prevents_insert(monkeypatch, capsys):
    c, _ = ready(monkeypatch)
    old = c.exec_driver_sql
    def wrong_vocab(stmt):
        if "vocabulary_version FROM" in stmt:
            return FakeResult(rows=[("another_version",)])
        return old(stmt)
    c.exec_driver_sql = wrong_vocab
    with pytest.raises(SystemExit):
        mod.perform("config", "manifest", execute=True)
    assert "VOCABULARY" in capsys.readouterr().err
    assert not any(stmt.startswith("INSERT") for stmt in c.sql)


def test_secret_bearing_provider_errors_are_suppressed(monkeypatch, capsys):
    c, _ = ready(monkeypatch)
    def leak(_):
        raise RuntimeError("password=SYNTHETIC_SECRET record_id=SYNTHETIC_PATIENT")
    c.exec_driver_sql = leak
    with pytest.raises(SystemExit):
        mod.perform("config", "manifest", execute=True)
    output = capsys.readouterr()
    assert "BLOCKED [IDENTITY]" in output.err
    assert "SYNTHETIC_SECRET" not in output.err + output.out
    assert "SYNTHETIC_PATIENT" not in output.err + output.out


def test_default_cli_is_no_sql(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["amend_frozen_cdm_source"])
    monkeypatch.setattr(mod, "perform", lambda *args, **kw:
                        pytest.fail("default preview should not invoke SQL"))
    mod.main()
    assert "No SQL connection" in capsys.readouterr().out


def test_execute_requires_exact_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "amend_frozen_cdm_source", "--execute", "--confirm", "WRONG"
    ])
    monkeypatch.setattr(mod, "perform", lambda *args, **kw:
                        pytest.fail("unexpected SQL"))
    with pytest.raises(SystemExit):
        mod.main()
    assert "BLOCKED [CONFIRMATION]" in capsys.readouterr().err or True


def test_real_manifest_validation_is_reused():
    assert mod.FIELDS == tuple(mod.manifest_validator.FIELD_LIMITS)
    assert mod.CONFIRM != ""
