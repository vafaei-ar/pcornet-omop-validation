from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision_postfreeze_qc_access.py"
SPEC = importlib.util.spec_from_file_location("qc_provision", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


def fixture_config(tmp_path, server="localhost", cdm="5.4.2"):
    config = {
        "etl": {"cdm_version": cdm, "backend": "sqlserver"},
        "source": {"parquet_dir": "/none"},
        "sqlserver": {
            "server": server,
            "database": "OMOP_VALIDATED",
            "target_schema": "dbo",
            "user": "SA",
            "password_env": "TEST_ADMIN_PASSWORD",
        },
        "vocabulary": {"directory": "/none"},
        "output": {"parquet_dir": "/none"},
    }
    path = tmp_path / "etl.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_dry_run_creates_isolated_plan_without_db_access(tmp_path, monkeypatch):
    cfg = fixture_config(tmp_path)
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "synthetic-test-admin-secret")
    plan = provision.plan_from_config(cfg)
    assert plan == {
        "target_db": "OMOP_VALIDATED",
        "target_schema": "dbo",
        "qc_db": "OMOP_VALIDATED_postfreeze_qc",
        "qc_login": "ohdsi_postfreeze_qc",
        "results_schema": "qc_results",
        "scratch_schema": "qc_scratch",
    }


def test_rejects_remote_server_to_avoid_unintended_provisioning(tmp_path, monkeypatch):
    cfg = fixture_config(tmp_path, server="remote.production.example")
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "synthetic-test-admin-secret")
    with pytest.raises(ValueError, match="known local SQL Server"):
        provision.plan_from_config(cfg)


def test_rejects_wrong_frozen_version(tmp_path, monkeypatch):
    cfg = fixture_config(tmp_path, cdm="5.4.1")
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "synthetic-test-admin-secret")
    with pytest.raises(ValueError, match="5.4.2"):
        provision.plan_from_config(cfg)


def test_rejects_missing_admin_password(tmp_path, monkeypatch):
    cfg = fixture_config(tmp_path)
    monkeypatch.delenv("TEST_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="admin password"):
        provision.plan_from_config(cfg)


def test_password_prompt_never_returns_mismatched_secret(monkeypatch):
    responses = iter(["syntheticSecret987$AA", "not-the-same"])
    monkeypatch.setattr(provision.getpass, "getpass", lambda prompt: next(responses))
    with pytest.raises(ValueError, match="Passwords do not match"):
        provision.password_prompt()


def test_sql_identifiers_are_restricted():
    for candidate in ("dbo;DROP", "database.schema", "space name", "a]"):
        with pytest.raises(ValueError, match="Unsafe"):
            provision.checked_identifier(candidate)
    assert provision.checked_identifier("OMOP_VALIDATED_2026") == "OMOP_VALIDATED_2026"
