from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight_postfreeze_omop_qc.py"
SPEC = importlib.util.spec_from_file_location("postfreeze_qc_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_inputs(tmp_path, cdm_version="5.4.2"):
    cfg = tmp_path / "local-etl.yaml"
    cfg.write_text(yaml.safe_dump({
        "etl": {"cdm_version": cdm_version, "backend": "sqlserver"},
        "source": {"parquet_dir": "/nonexistent"},
        "sqlserver": {
            "server": "localhost", "database": "OMOP_VALIDATED",
            "user": "QC_READ", "password_env": "LOCAL_TEST_OMOP_PASSWORD",
            "target_schema": "dbo", "trust_server_certificate": True,
        },
        "vocabulary": {"directory": "/nonexistent"},
        "output": {"parquet_dir": "/nonexistent"},
    }), encoding="utf-8")
    driver = tmp_path / "drivers"
    driver.mkdir()
    (driver / "mssql-jdbc-test.jar").write_bytes(b"synthetic fixture only")
    return str(cfg), str(driver)


def test_uses_local_config_and_password_environment_without_disk_export(tmp_path, monkeypatch):
    cfg, jar = make_inputs(tmp_path)
    monkeypatch.setenv("LOCAL_TEST_OMOP_PASSWORD", "synthetic-example-password")
    env = module.build_environment(cfg, jar)
    assert env["OHDSI_DB_NAME"] == "OMOP_VALIDATED"
    assert env["OHDSI_DB_SERVER"] == "localhost"
    assert env["OHDSI_DB_USER"] == "QC_READ"
    assert env["OHDSI_DB_PASSWORD"] == "synthetic-example-password"
    assert env["OHDSI_TARGET_SCHEMA"] == "dbo"
    assert env["OHDSI_TLS_TRUST_SERVER_CERTIFICATE"] == "true"
    assert not list(tmp_path.rglob("*password*"))


def test_does_not_connect_when_password_environment_missing(tmp_path, monkeypatch):
    cfg, jar = make_inputs(tmp_path)
    monkeypatch.delenv("LOCAL_TEST_OMOP_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="Password environment variable"):
        module.build_environment(cfg, jar)


def test_rejects_unfrozen_cdm_version(tmp_path, monkeypatch):
    cfg, jar = make_inputs(tmp_path, cdm_version="5.3")
    monkeypatch.setenv("LOCAL_TEST_OMOP_PASSWORD", "synthetic-example-password")
    with pytest.raises(ValueError, match=r"OMOP CDM 5\.4"):
        module.build_environment(cfg, jar)


def test_odbc_diagnostic_is_select_only(tmp_path, monkeypatch, capsys):
    from pcornet_omop_validation.etl import database

    cfg, _ = make_inputs(tmp_path)
    monkeypatch.setenv("LOCAL_TEST_OMOP_PASSWORD", "synthetic-example-password")
    calls = []

    class ScalarResult:
        def scalar_one(self):
            return "OMOP_VALIDATED"

    class ReadOnlyConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            calls.append(str(statement))
            return ScalarResult()

    class MockEngine:
        def connect(self):
            return ReadOnlyConnection()

        def dispose(self):
            calls.append("DISPOSE")

    monkeypatch.setattr(database, "make_engine", lambda cfg: MockEngine())
    assert module.diagnose_odbc(cfg) == 0
    assert calls == ["SELECT DB_NAME()", "DISPOSE"]
    output = capsys.readouterr().out
    assert "connection and database identity PASS" in output
    assert "synthetic-example-password" not in output


def test_odbc_failure_never_prints_provider_secrets(tmp_path, monkeypatch, capsys):
    from pcornet_omop_validation.etl import database

    cfg, _ = make_inputs(tmp_path)
    monkeypatch.setenv("LOCAL_TEST_OMOP_PASSWORD", "synthetic-example-password")

    class FailingEngine:
        def connect(self):
            raise RuntimeError(
                "Login failed for user QC_READ; PWD=synthetic-example-password"
            )

        def dispose(self):
            pass

    monkeypatch.setattr(database, "make_engine", lambda cfg: FailingEngine())
    assert module.diagnose_odbc(cfg) == 1
    captured = capsys.readouterr()
    assert "SQL Server login rejected" in captured.err
    assert "synthetic-example-password" not in captured.err
    assert "QC_READ" not in captured.err


def test_restricted_qc_jdbc_uses_separate_env_password(tmp_path, monkeypatch):
    cfg, jar = make_inputs(tmp_path)
    monkeypatch.delenv("LOCAL_TEST_OMOP_PASSWORD", raising=False)
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic-qc-secret")
    env = module.build_environment(cfg, jar, qc_login=True)
    assert env["OHDSI_DB_USER"] == "ohdsi_postfreeze_qc"
    assert env["OHDSI_DB_PASSWORD"] == "synthetic-qc-secret"
    assert env["OHDSI_DB_NAME"] == "OMOP_VALIDATED"


def test_restricted_qc_jdbc_never_falls_back_to_sa_password(tmp_path, monkeypatch):
    cfg, jar = make_inputs(tmp_path)
    monkeypatch.setenv("LOCAL_TEST_OMOP_PASSWORD", "synthetic-admin-secret")
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="OHDSI_QC_PASSWORD"):
        module.build_environment(cfg, jar, qc_login=True)



def test_qc_access_audit_uses_only_restricted_login_and_read_only_script(
    tmp_path, monkeypatch,
):
    import sys

    cfg, jar = make_inputs(tmp_path)
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic-qc-secret")
    monkeypatch.delenv("LOCAL_TEST_OMOP_PASSWORD", raising=False)
    called = []

    def capture_run(command, *, check, env):
        called.append((command, check, env))

    monkeypatch.setattr(module.subprocess, "run", capture_run)
    monkeypatch.setattr(sys, "argv", [
        "preflight_postfreeze_omop_qc.py",
        "--config", cfg, "--driver-dir", jar,
        "--qc-login", "--audit-qc-access",
    ])
    module.main()
    assert len(called) == 1
    command, strict, env = called[0]
    assert strict is True
    assert command[0:2] == ["Rscript", "--vanilla"]
    assert command[2].endswith("audit_postfreeze_qc_permissions.R")
    assert env["OHDSI_DB_USER"] == "ohdsi_postfreeze_qc"
    assert env["OHDSI_DB_PASSWORD"] == "synthetic-qc-secret"


def test_qc_access_audit_requires_restricted_login(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", [
        "preflight_postfreeze_omop_qc.py", "--audit-qc-access",
    ])
    with pytest.raises(SystemExit, match="requires --qc-login"):
        module.main()
