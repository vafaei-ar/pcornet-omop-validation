from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_frozen_omop.py"
SPEC = importlib.util.spec_from_file_location("backup_frozen_omop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def test_backup_path_is_confined_to_protected_sql_server_directory():
    path = backup.build_backup_path(
        datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc),
        token="abcdef0123",
    )
    assert path == (
        Path("/var/opt/mssql/backup")
        / "OMOP_VALIDATED_A_copyonly_20261030T210000Z_abcdef0123.bak"
    )


def test_rejects_unsafe_backup_filename_token():
    with pytest.raises(ValueError, match="Invalid backup"):
        backup.build_backup_path(
            datetime(2026, 10, 30, tzinfo=timezone.utc),
            token="foo'--",
        )


def test_backup_waits_for_all_pyodbc_status_results_before_closing():
    events = []
    results = iter([True, True, True, False])

    class FakeCursor:
        def execute(self, sql):
            events.append("execute:" + sql)
            return self

        def nextset(self):
            events.append("nextset")
            return next(results)

        def close(self):
            events.append("close")

    class FakeConnection:
        def cursor(self):
            events.append("cursor")
            return FakeCursor()

    backup.drain_dbapi_results(FakeConnection(), "BACKUP DATABASE [OMOP_VALIDATED_A]")
    assert events[0] == "cursor"
    assert events[1].startswith("execute:BACKUP DATABASE")
    assert events[2:6] == ["nextset"] * 4
    assert events[6] == "close"


def test_backup_cursor_closed_on_provider_error_without_faking_completion():
    events = []

    class FakeCursor:
        def execute(self, sql):
            events.append("execute")

        def nextset(self):
            events.append("nextset")
            raise RuntimeError("Simulated provider failure")

        def close(self):
            events.append("close")

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    with pytest.raises(RuntimeError, match="Simulated provider failure"):
        backup.drain_dbapi_results(FakeConnection(), "BACKUP DATABASE")
    assert events == ["execute", "nextset", "close"]


def test_preview_does_not_call_execute(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["backup_frozen_omop.py"])
    monkeypatch.setattr(
        backup,
        "execute_backup",
        lambda path: pytest.fail("Preview must not open a DB connection"),
    )
    backup.main()
    output = capsys.readouterr().out
    assert "PREVIEW ONLY" in output
    assert "No SQL connection" in output


def test_service_owned_directory_permission_error_does_not_block_backup(monkeypatch, capsys):
    """The operator lacks access, but mssql's own write access was checked separately."""
    original = Path.is_dir

    def protected_directory(self):
        if self == backup.BACKUP_DIRECTORY:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "is_dir", protected_directory)
    backup.verify_backup_directory_precondition()
    assert "protected from this login" in capsys.readouterr().out


def test_missing_backup_directory_blocks_execution(monkeypatch):
    original = Path.is_dir

    def missing_directory(self):
        if self == backup.BACKUP_DIRECTORY:
            return False
        return original(self)

    monkeypatch.setattr(Path, "is_dir", missing_directory)
    with pytest.raises(RuntimeError, match="does not exist"):
        backup.verify_backup_directory_precondition()
