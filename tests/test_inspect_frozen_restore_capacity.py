"""No-SQL synthetic checks of protected backup FILELISTONLY planning."""
from __future__ import annotations

from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from pcornet_omop_validation.study import postfreeze_model_interpretability as m

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_frozen_restore_capacity.py"
spec = importlib.util.spec_from_file_location("restore_capacity_script", SCRIPT)
assert spec is not None and spec.loader is not None
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)

BACKUP = (
    "/var/opt/mssql/backup/"
    "OMOP_VALIDATED_A_copyonly_20261030T103000Z_0123456789.bak"
)


def test_backup_history_rejects_missing_ambiguous_or_unsafe_media():
    row = {
        "physical_device_name": BACKUP,
        "position": 1,
        "compressed_backup_size": 23 * capacity.GIB,
    }
    assert capacity.verified_backup_history([row]) == (
        BACKUP, 1, 23 * capacity.GIB
    )
    with pytest.raises(capacity.CapacityBlocked, match="Ambiguous"):
        capacity.verified_backup_history([])
    with pytest.raises(capacity.CapacityBlocked, match="Ambiguous"):
        capacity.verified_backup_history([row, row])
    for field, value in [
        ("physical_device_name", "/tmp/OMOP_VALIDATED_A_copyonly_bad.bak"),
        ("physical_device_name", BACKUP + "';RESTORE DATABASE OTHER;--"),
        ("position", 0),
        ("compressed_backup_size", 0),
    ]:
        bad = dict(row)
        bad[field] = value
        with pytest.raises(capacity.CapacityBlocked):
            capacity.verified_backup_history([bad])


def test_filelist_accepts_decimal_sizes_and_sums_allocations():
    rows = [
        {"LogicalName": "synthetic_data", "Type": "D",
         "Size": Decimal(8 * capacity.GIB), "IsPresent": True},
        {"LogicalName": "synthetic_data2", "Type": "D",
         "Size": Decimal(capacity.GIB), "IsPresent": 1},
        {"LogicalName": "synthetic_log", "Type": "L",
         "Size": Decimal(2 * capacity.GIB), "IsPresent": True},
    ]
    summary = capacity.summarize_filelist(rows)
    assert summary == {
        "file_count": 3,
        "data_bytes": 9 * capacity.GIB,
        "log_bytes": 2 * capacity.GIB,
        "other_bytes": 0,
        "total_bytes": 11 * capacity.GIB,
    }


@pytest.mark.parametrize("value", [
    -1, 1.2, Decimal("-1"), Decimal("1.2"),
    Decimal("NaN"), Decimal("Infinity"), None, True, "123",
])
def test_filelist_rejects_invalid_sizes(value):
    rows = [
        {"Type": "D", "Size": value, "IsPresent": True},
        {"Type": "L", "Size": 10, "IsPresent": True},
    ]
    with pytest.raises(capacity.CapacityBlocked):
        capacity.summarize_filelist(rows)


def test_filelist_rejects_missing_log_or_unavailable_file():
    with pytest.raises(capacity.CapacityBlocked, match="log"):
        capacity.summarize_filelist(
            [{"Type": "D", "Size": 100, "IsPresent": True}]
        )
    with pytest.raises(capacity.CapacityBlocked, match="incomplete"):
        capacity.summarize_filelist([
            {"Type": "D", "Size": 100, "IsPresent": False},
            {"Type": "L", "Size": 10, "IsPresent": True},
        ])


class FakeResult:
    def __init__(self, data):
        self.data = data

    def scalar_one(self):
        return self.data

    def mappings(self):
        return self

    def all(self):
        return self.data


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execution_options(self, **kwargs):
        assert kwargs == {"isolation_level": "AUTOCOMMIT"}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        stmt = str(query).strip()
        self.statements.append(stmt)
        if stmt == "SELECT DB_NAME()":
            return FakeResult("OMOP_VALIDATED_A")
        assert stmt.startswith("SELECT bs.position")
        assert params["database_name"] == "OMOP_VALIDATED_A"
        return FakeResult([{
            "position": 1,
            "physical_device_name": BACKUP,
            "compressed_backup_size": 23 * capacity.GIB,
        }])

    def exec_driver_sql(self, sql):
        self.statements.append(sql)
        assert sql.startswith("RESTORE FILELISTONLY FROM DISK")
        assert "WITH FILE = 1" in sql
        assert "RESTORE DATABASE" not in sql
        return FakeResult([
            {"Type": "D", "Size": Decimal(130 * capacity.GIB),
             "IsPresent": True},
            {"Type": "L", "Size": Decimal(20 * capacity.GIB),
             "IsPresent": True},
        ])


def test_only_readonly_metadata_queries_used(monkeypatch, capsys):
    conn = FakeConnection()
    class Engine:
        def connect(self):
            return conn
        def dispose(self):
            self.disposed = True
    engine = Engine()
    monkeypatch.setattr(capacity, "verified_server_config", lambda _: {})
    monkeypatch.setattr(capacity, "load_etl_config",
                        lambda _: SimpleNamespace(sql_password="synthetic"))
    monkeypatch.setattr(capacity, "make_engine", lambda cfg, database: engine)
    monkeypatch.setattr(capacity.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=811 * capacity.GIB))
    capacity.inspect("synthetic-config.yaml")
    visible = capsys.readouterr()
    assert "FROZEN BACKUP FILELISTONLY CAPACITY: PASS" in visible.out
    assert "Total logical allocation: 150.00 GiB" in visible.out
    assert "Current local free space: 811.00 GiB" in visible.out
    assert BACKUP not in visible.out + visible.err
    assert engine.disposed
    assert len(conn.statements) == 3
    assert all(q.startswith(("SELECT", "RESTORE FILELISTONLY"))
               for q in conn.statements)


def test_cli_suppresses_sql_credentials_and_details(monkeypatch, capsys):
    def unsafe(_):
        raise RuntimeError("PWD={synthetic_secret}; PATID=synthetic_id")
    monkeypatch.setattr(capacity, "inspect", unsafe)
    monkeypatch.setattr(sys, "argv", ["inspect_frozen_restore_capacity"])
    with pytest.raises(SystemExit) as stopped:
        capacity.main()
    assert stopped.value.code == 1
    visible = capsys.readouterr()
    assert "FROZEN BACKUP FILELISTONLY CAPACITY: BLOCKED" in visible.err
    assert "synthetic_secret" not in visible.out + visible.err
    assert "synthetic_id" not in visible.out + visible.err
