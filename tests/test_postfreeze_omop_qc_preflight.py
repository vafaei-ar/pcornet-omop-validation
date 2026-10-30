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
    with pytest.raises(ValueError, match="v5.4"):
        module.build_environment(cfg, jar)
