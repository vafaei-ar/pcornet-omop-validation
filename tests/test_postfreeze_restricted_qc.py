"""Synthetic guards for the importable restricted frozen-target QC policy.

No database connection, institutional data or credentials.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pcornet_omop_validation.study import postfreeze_restricted_qc as qc


def test_restricted_qc_import_works_from_installed_package():
    assert qc.FROZEN_DATABASE == "OMOP_VALIDATED_A"
    assert qc.QC_LOGIN == "ohdsi_postfreeze_qc"
    assert qc.FROZEN_COUNT_ANCHORS == {
        "person": 27089,
        "visit_occurrence": 1510957,
        "condition_occurrence": 7315572,
    }


def test_config_checks_target_host_and_driver(monkeypatch):
    data = {
        "etl": {"backend": "sqlserver", "cdm_version": "5.4.2"},
        "sqlserver": {
            "database": "OMOP_VALIDATED_A",
            "server": "localhost",
            "driver": "ODBC Driver 18 for SQL Server",
            "target_schema": "dbo",
            "trust_server_certificate": True,
        },
    }
    monkeypatch.setattr(qc, "load_etl_config", lambda _: SimpleNamespace(raw=data))
    assert qc.verified_server_config("synthetic.yaml") == {
        "server": "localhost",
        "driver": "ODBC Driver 18 for SQL Server",
        "trust": "yes",
    }
    for key, val, message in (
        ("database", "WRONG", "frozen database"),
        ("server", "remote.example", "outside"),
        ("driver", "Untrusted ODBC", "driver"),
        ("target_schema", "unlocked", "schema"),
    ):
        original = data["sqlserver"][key]
        data["sqlserver"][key] = val
        with pytest.raises(qc.AuditBlocked, match=message):
            qc.verified_server_config("synthetic.yaml")
        data["sqlserver"][key] = original


def test_odbc_requires_allowed_database_and_restricted_user():
    cfg = {"driver": "ODBC Driver 18 for SQL Server",
           "server": "localhost", "trust": "yes"}
    with pytest.raises(qc.AuditBlocked, match="Unexpected database"):
        qc.make_restricted_odbc_string(cfg, "master", "synthetic")
    with pytest.raises(qc.AuditBlocked, match="PASSWORD"):
        qc.make_restricted_odbc_string(cfg, qc.FROZEN_DATABASE, "")
    dsn = qc.make_restricted_odbc_string(
        cfg, qc.FROZEN_DATABASE, "test_only;value}",
    )
    assert "UID=ohdsi_postfreeze_qc" in dsn
    assert "DATABASE=OMOP_VALIDATED_A" in dsn
    assert "PWD={test_only;value}}}" in dsn


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.closed = False

    def execute(self, sql):
        self.connection.sql.append(sql)
        self.row = self.connection.responses[sql]

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.sql = []
        self.cursors = []
        self.responses = {
            "SELECT DB_NAME(), ORIGINAL_LOGIN()":
                (qc.FROZEN_DATABASE, qc.QC_LOGIN),
            **{
                f"SELECT COUNT_BIG(*) FROM [dbo].[{table}]": (n,)
                for table, n in qc.FROZEN_COUNT_ANCHORS.items()
            },
            "SELECT COUNT_BIG(*) FROM [dbo].[cdm_source]": (0,),
        }

    def cursor(self):
        c = Cursor(self)
        self.cursors.append(c)
        return c


def test_frozen_check_validates_login_anchor_counts_and_cdm_source(capsys):
    connection = Connection()
    qc.inspect_frozen(connection)
    out = capsys.readouterr().out
    assert "Frozen publication row-count anchors: PASS" in out
    assert "Frozen CDM_SOURCE: 0 rows" in out
    assert all(c.closed for c in connection.cursors)
    assert len(connection.sql) == 5


@pytest.mark.parametrize("sql,wrong", [
    ("SELECT DB_NAME(), ORIGINAL_LOGIN()", ("OMOP_VALIDATED_A", "SA")),
    ("SELECT COUNT_BIG(*) FROM [dbo].[person]", (1,)),
    ("SELECT COUNT_BIG(*) FROM [dbo].[cdm_source]", (1,)),
])
def test_frozen_guard_blocks_identity_or_count_changes(sql, wrong):
    connection = Connection()
    connection.responses[sql] = wrong
    with pytest.raises(qc.AuditBlocked):
        qc.inspect_frozen(connection)
    assert all(c.closed for c in connection.cursors)


def test_read_guard_rejects_non_select():
    connection = Connection()
    with pytest.raises(qc.AuditBlocked, match="Only SELECT"):
        qc.single_scalar(connection, "DELETE FROM dbo.person")
    assert not connection.sql
