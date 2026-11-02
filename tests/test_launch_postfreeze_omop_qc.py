from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "launch_postfreeze_omop_qc.py"
SPEC = importlib.util.spec_from_file_location("local_qc_launcher", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_fixture(tmp_path):
    config_path = tmp_path / "etl.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "etl": {"cdm_version": "5.4.2", "backend": "sqlserver"},
            "source": {"parquet_dir": "/no-data"},
            "sqlserver": {
                "server": "localhost", "database": "OMOP_VALIDATED_A",
                "target_schema": "dbo", "user": "SA",
                "password_env": "OMOP_SQL_PASSWORD",
                "trust_server_certificate": True,
            },
            "vocabulary": {"directory": "/no-data"},
            "output": {"parquet_dir": "/no-data"},
        }), encoding="utf-8",
    )
    driver_dir = tmp_path / "drivers"
    driver_dir.mkdir()
    (driver_dir / "mssql-test.jar").write_bytes(b"not a real driver")
    return str(config_path), str(driver_dir)


def test_preview_requires_no_password_and_runs_no_database_commands(
    tmp_path, monkeypatch, capsys
):
    cfg, driver = make_fixture(tmp_path)
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Preview must not start R"),
    )
    module.run_qc(cfg, driver, "dqd", execute=False)
    output = capsys.readouterr().out
    assert "PREVIEW ONLY" in output
    assert "writeToTable=FALSE" in output


def test_build_environment_excludes_admin_credentials(tmp_path, monkeypatch):
    cfg_path, driver_dir = make_fixture(tmp_path)
    monkeypatch.setenv("OMOP_SQL_PASSWORD", "synthetic-admin-secret")
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic-qc-secret")
    config = module.validate_target(cfg_path)
    env = module.build_environment(config, driver_dir, "dqd")
    assert "OMOP_SQL_PASSWORD" not in env
    assert env["OHDSI_DB_USER"] == "ohdsi_postfreeze_qc"
    assert env["OHDSI_DB_PASSWORD"] == "synthetic-qc-secret"
    assert env["OHDSI_CDM_SCHEMA"] == "OMOP_VALIDATED_A.dbo"
    assert env["OHDSI_RESULTS_SCHEMA"] == "OMOP_VALIDATED_A_postfreeze_qc.qc_results"
    assert env["OHDSI_SCRATCH_SCHEMA"] == "OMOP_VALIDATED_A_postfreeze_qc.qc_scratch"
    assert env["OHDSI_QC_EXPECTED_PACKAGE_VERSION"] == "2.9.1"
    assert env["OHDSI_QC_CONFIRM"] == module.CONFIRMATION
    assert "synthetic-admin-secret" not in str(env)


def test_frozen_database_mismatch_blocks_before_launch(tmp_path, monkeypatch):
    cfg_path, driver_dir = make_fixture(tmp_path)
    text = Path(cfg_path).read_text(encoding="utf-8").replace(
        "OMOP_VALIDATED_A", "OTHER_DB"
    )
    Path(cfg_path).write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="non-frozen"):
        module.run_qc(cfg_path, driver_dir, "dqd", execute=False)


def test_execute_rejects_missing_qc_password(tmp_path, monkeypatch):
    cfg, driver = make_fixture(tmp_path)
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    monkeypatch.setattr(module, "check_new_output_location", lambda tool, run_label="": None)
    with pytest.raises(ValueError, match="OHDSI_QC_PASSWORD"):
        module.run_qc(cfg, driver, "dqd", execute=True)


def test_execution_audits_permissions_before_dqd(tmp_path, monkeypatch):
    cfg, driver = make_fixture(tmp_path)
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic-qc-secret")
    monkeypatch.setenv("OMOP_SQL_PASSWORD", "synthetic-admin-secret")
    monkeypatch.setattr(module, "check_new_output_location", lambda tool, run_label="": None)
    monkeypatch.setattr(module.os, "umask", lambda value: None)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.run_qc(cfg, driver, "dqd", execute=True)
    assert len(calls) == 2
    assert calls[0][0][-1].endswith("audit_postfreeze_qc_permissions.R")
    assert calls[1][0][-2].endswith("run_postfreeze_omop_qc.R")
    assert calls[1][0][-1] == "dqd"
    for _, kwargs in calls:
        assert kwargs["check"] is True
        assert kwargs["cwd"] == module.ROOT
        assert kwargs["env"]["OHDSI_DB_USER"] == "ohdsi_postfreeze_qc"
        assert "OMOP_SQL_PASSWORD" not in kwargs["env"]


def test_existing_private_dqd_output_prevents_overwrite(tmp_path, monkeypatch):
    output = tmp_path / "dqd"
    output.mkdir()
    (output / "dqd_results.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "planned_output", lambda tool: output)
    with pytest.raises(ValueError, match="Previous QC output files"):
        module.check_new_output_location("dqd")


def test_unknown_tool_cannot_bypass_version_pin(tmp_path):
    with pytest.raises(ValueError, match="Unknown QC tool"):
        module.planned_output("arbitrary-sql")


def test_named_dqd_rerun_uses_new_directory_and_preserves_original(tmp_path, monkeypatch):
    first = tmp_path / "dqd"
    first.mkdir()
    (first / "dqd_results.json").write_text("SYNTHETIC_ORIGINAL", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    label = "cohort_schema_fix_v1"
    first_path = module.planned_output("dqd")
    assert first_path.name == "dqd"
    new_path = module.planned_output("dqd", label)
    assert new_path.name == "dqd_cohort_schema_fix_v1"
    assert new_path != first_path
    module.check_new_output_location("dqd", label)
    assert (first / "dqd_results.json").read_text() == "SYNTHETIC_ORIGINAL"


@pytest.mark.parametrize("label", ["../dqd", "/tmp", " ", "bad.path", "a/b", "a" * 65])
def test_invalid_dqd_rerun_label_is_rejected(label):
    with pytest.raises(ValueError, match="Invalid DQD run label"):
        module.planned_output("dqd", label)


def test_achilles_rerun_label_is_rejected():
    with pytest.raises(ValueError, match="Only DQD"):
        module.planned_output("achilles", "new_run")


def test_named_dqd_environment_passes_only_restricted_output_subdir(
    tmp_path, monkeypatch,
):
    cfg_path, driver_dir = make_fixture(tmp_path)
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic-qc-secret")
    env = module.build_environment(
        module.validate_target(cfg_path), driver_dir, "dqd",
        run_label="cohort_schema_fix_v1"
    )
    assert env["OHDSI_QC_OUTPUT_SUBDIR"] == "dqd_cohort_schema_fix_v1"
    assert "OMOP_SQL_PASSWORD" not in env


def test_r_runner_pins_cohort_schema_and_checks_run_error_reports():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_postfreeze_omop_qc.R"
    ).read_text(encoding="utf-8")
    assert "cohortDatabaseSchema = cdm_schema" in source
    assert "OHDSI_QC_OUTPUT_SUBDIR" in source
    assert 'stop("Refusing to overwrite a previous QC run")' in source
    assert "dqd_error_report_count" in source
    assert "dqd_sql_errors_absent" in source
    assert "writeToTable = FALSE" in source
    assert "dqd_error_reports > 0L" in source


def test_existing_named_dqd_output_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    out = module.planned_output("dqd", "cohort_schema_fix_v1")
    out.mkdir(parents=True)
    (out / "execution_manifest.json").write_text("SYNTHETIC_EXISTING")
    with pytest.raises(ValueError, match="Previous QC output files"):
        module.check_new_output_location("dqd", "cohort_schema_fix_v1")
