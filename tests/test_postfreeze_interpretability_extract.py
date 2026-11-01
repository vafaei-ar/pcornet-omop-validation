from __future__ import annotations

import pytest
import hashlib
import json
from types import SimpleNamespace
import stat
from pathlib import Path

pytest.importorskip("sqlalchemy")
pd = pytest.importorskip("pandas")

from pcornet_omop_validation.study import postfreeze_interpretability_extract as extractor


def test_row_level_export_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="--confirm-private"):
        extractor.run("does-not-exist.yaml", "results/private_inputs", False)


def test_export_refuses_path_outside_ignored_results(tmp_path):
    with pytest.raises(ValueError, match="under ignored results"):
        extractor.run("does-not-exist.yaml", str(tmp_path / "private"), True)


def test_frozen_sha_unchanged():
    assert extractor.FROZEN_SHA == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"



def test_private_export_path_is_inside_repo_results_only(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "results").mkdir(parents=True)
    monkeypatch.setattr(extractor, "PRIVATE_ROOT", root / "results")
    good = root / "results" / "private_inputs"
    assert extractor._private_output_path(str(good)) == good
    with pytest.raises(ValueError, match="under ignored results"):
        extractor._private_output_path(str(tmp_path / "results" / "private_inputs"))
    with pytest.raises(ValueError, match="under ignored results"):
        extractor._private_output_path(str(root / "results"))
    target = tmp_path / "outside"
    target.mkdir()
    (root / "results" / "alias").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        extractor._private_output_path(str(root / "results" / "alias" / "cohorts"))


def test_private_export_directory_fails_closed(tmp_path, monkeypatch):
    out = tmp_path / "private_inputs"
    out.mkdir(mode=0o700)
    out.chmod(0o700)
    work = extractor._prepare_private_output(out)
    assert work == out / "private_rerun"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError, match="not empty"):
        extractor._prepare_private_output(out)


def test_private_export_rejects_group_or_world_readable_directory(tmp_path):
    out = tmp_path / "private_inputs"
    out.mkdir()
    out.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        extractor._prepare_private_output(out)


def test_private_parquet_file_uses_0600_and_refuses_overwrite(
    tmp_path, monkeypatch,
):
    path = tmp_path / "synthetic.parquet"
    frame = pd.DataFrame({"feature": [1, 2]})
    def fake_to_parquet(self, handle, index):
        assert index is False
        handle.write(b"synthetic-only-not-a-parquet")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", fake_to_parquet)
    extractor._write_private_parquet(frame, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"synthetic-only-not-a-parquet"
    with pytest.raises(FileExistsError):
        extractor._write_private_parquet(frame, path)


def test_private_population_rejects_null_and_duplicate_identifiers(monkeypatch):
    monkeypatch.setitem(extractor.EXPECTED, "pcornet_full", (3, 1))
    cols = {
        "patid": ["p1", "p2", "p3"],
        "index_date": ["2024-01-01"] * 3,
        "outcome": [0, 1, 0],
        **{
            name: ([0.0, 1.0, 0.0] if name in (
                "female_indicator", "prior_365d_ischemic_stroke_indicator"
            ) else [1.0, 2.0, 3.0])
            for name in extractor.FEATURES
        },
    }
    clean = pd.DataFrame(cols)
    parsed = extractor._prepare_population(clean, "pcornet_full")
    assert list(parsed.index) == cols["patid"]
    duplicated = clean.copy()
    duplicated.loc[1, "patid"] = "p1"
    with pytest.raises(RuntimeError, match="duplicated"):
        extractor._prepare_population(duplicated, "pcornet_full")
    missing = clean.copy()
    missing.loc[1, "patid"] = None
    with pytest.raises(RuntimeError, match="null patient"):
        extractor._prepare_population(missing, "pcornet_full")
    null_date = clean.copy()
    null_date.loc[1, "index_date"] = None
    with pytest.raises(RuntimeError, match="index date"):
        extractor._prepare_population(null_date, "pcornet_full")
    malformed = clean.copy()
    malformed.loc[0, "female_indicator"] = 2
    with pytest.raises(RuntimeError, match="binary feature"):
        extractor._prepare_population(malformed, "pcornet_full")
    infinite = clean.copy()
    infinite.loc[0, "index_length_of_stay_days"] = float("inf")
    with pytest.raises(RuntimeError, match="non-finite"):
        extractor._prepare_population(infinite, "pcornet_full")



def _fake_locked_local_inputs(tmp_path, monkeypatch):
    study = tmp_path / "study.json"
    d0 = tmp_path / "d0.json"
    stage_d = tmp_path / "stage_d.json"
    for item in (study, d0, stage_d):
        item.write_text("{}", encoding="utf-8")
    for attr, value in (
        ("STUDY_PATH", study), ("D0_PATH", d0),
        ("STAGE_D_PATH", stage_d),
    ):
        monkeypatch.setattr(extractor.original_stage_e, attr, value)
    preflight_dir = (
        tmp_path / "publication_analysis" / "stage_e_statistical_model"
    )
    preflight_dir.mkdir(parents=True)
    preflight = preflight_dir / "stage_e_statistical_model_preflight.json"
    preflight.write_text(json.dumps({
        "status": "stage_e_statistical_model_preflight_ready",
        "outcome_query_performed": False,
        "model_fit_performed": False,
        "study_definition_sha256": hashlib.sha256(study.read_bytes()).hexdigest(),
        "inherited_d0_definition_sha256": hashlib.sha256(d0.read_bytes()).hexdigest(),
        "inherited_stage_d_definition_sha256": hashlib.sha256(stage_d.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    fallback = tmp_path / "fallback-complete.json"
    fallback.write_text(json.dumps({
        "status": "stage_c_encounter_date_fallback_sensitivity_complete",
        "frozen_etl_sha": extractor.FROZEN_SHA,
    }), encoding="utf-8")
    monkeypatch.setattr(extractor.stage_d_fallback, "STAGE_C_RESULT_PATH", fallback)
    monkeypatch.setattr(
        extractor, "load_etl_config",
        lambda c: SimpleNamespace(
            raw={"etl": {"cdm_version": "5.4.2"}},
            audit_dir=tmp_path / "audit",
        ),
    )
    monkeypatch.setattr(extractor.importlib.metadata, "version", lambda _: "synthetic")
    return preflight


def test_preflight_only_uses_locked_local_evidence_no_sql_or_writes(
    tmp_path, monkeypatch,
):
    path = tmp_path / "results" / "private_inputs"
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(extractor, "PRIVATE_ROOT", tmp_path / "results")
    preflight = _fake_locked_local_inputs(tmp_path, monkeypatch)
    result = extractor.preflight_private_export(
        "config/etl_A.yaml", str(path)
    )
    assert result["status"] == "PRIVATE RF XGBOOST PREFLIGHT PASS"
    assert result["database_queries_performed"] is False
    assert result["private_files_created"] is False
    assert not path.exists()
    payload = json.loads(preflight.read_text(encoding="utf-8"))
    payload["study_definition_sha256"] = "invalid"
    preflight.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        extractor.preflight_private_export(
            "config/etl_A.yaml", str(path)
        )
    assert not path.exists()


def test_restricted_export_uses_qc_login_for_both_locked_stages(
    monkeypatch,
):
    import sys
    from urllib.parse import unquote_plus
    from pcornet_omop_validation.study import postfreeze_restricted_qc as qc

    events = []
    original_stage = extractor.original_stage_e.make_engine
    original_fallback = extractor.stage_d_fallback.make_engine

    class FakeConnection:
        def close(self):
            events.append("closed")
    class FakePyodbc:
        def connect(self, dsn, **kwargs):
            assert "UID=ohdsi_postfreeze_qc" in dsn
            assert kwargs["autocommit"] is True
            events.append("qc_connected")
            return FakeConnection()

    monkeypatch.setitem(sys.modules, "pyodbc", FakePyodbc())
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic_only")
    monkeypatch.setattr(qc, "verified_server_config", lambda _: {})
    monkeypatch.setattr(
        qc, "make_restricted_odbc_string",
        lambda *_: "UID=ohdsi_postfreeze_qc;PWD={synthetic_only};",
    )
    monkeypatch.setattr(qc, "inspect_frozen", lambda conn: events.append("checked"))

    def fake_engine(url, **kwargs):
        assert "UID=ohdsi_postfreeze_qc" in unquote_plus(url)
        assert "synthetic_only" in unquote_plus(url)
        events.append("engine")
        return object()

    monkeypatch.setattr(extractor, "create_engine", fake_engine)
    with extractor.restricted_stage_engines("config/etl_A.yaml"):
        assert extractor.original_stage_e.make_engine is not original_stage
        assert extractor.stage_d_fallback.make_engine is not original_fallback
        extractor.original_stage_e.make_engine(None)
        extractor.stage_d_fallback.make_engine(None)
    assert events == ["qc_connected", "checked", "closed", "engine", "engine"]
    assert extractor.original_stage_e.make_engine is original_stage
    assert extractor.stage_d_fallback.make_engine is original_fallback


def test_restricted_export_restores_origins_on_failure(monkeypatch):
    import sys
    from pcornet_omop_validation.study import postfreeze_restricted_qc as qc

    original_stage = extractor.original_stage_e.make_engine
    original_fallback = extractor.stage_d_fallback.make_engine
    class Connection:
        def close(self):
            pass
    class FakePyodbc:
        def connect(self, *args, **kwargs):
            return Connection()

    monkeypatch.setitem(sys.modules, "pyodbc", FakePyodbc())
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic_only")
    monkeypatch.setattr(qc, "verified_server_config", lambda _: {})
    monkeypatch.setattr(qc, "make_restricted_odbc_string", lambda *_: "synthetic")
    monkeypatch.setattr(qc, "inspect_frozen", lambda _: None)
    with pytest.raises(RuntimeError, match="forced synthetic"):
        with extractor.restricted_stage_engines("config/etl_A.yaml"):
            raise RuntimeError("forced synthetic")
    assert extractor.original_stage_e.make_engine is original_stage
    assert extractor.stage_d_fallback.make_engine is original_fallback


def test_restricted_export_requires_qc_password_before_sql(monkeypatch):
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    from pcornet_omop_validation.study import postfreeze_restricted_qc as qc
    monkeypatch.setattr(qc, "verified_server_config", lambda _: {})
    with pytest.raises(RuntimeError, match="OHDSI_QC_PASSWORD"):
        with extractor.restricted_stage_engines("config/etl_A.yaml"):
            pytest.fail("should not yield without credential")


def test_cli_suppresses_sensitive_provider_exceptions(
    monkeypatch, capsys,
):
    import sys
    private_error = "PWD={synthetic_secret}; PATID=synthetic_person"
    def failed_run(*args, **kwargs):
        raise RuntimeError(private_error)

    monkeypatch.setattr(extractor, "run", failed_run)
    monkeypatch.setattr(sys, "argv", [
        "postfreeze_interpretability_extract",
        "--config", "config/etl_A.yaml",
        "--output-dir", "results/private_inputs",
        "--confirm-private",
    ])
    with pytest.raises(SystemExit) as exc:
        extractor.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert private_error not in captured.out + captured.err
    assert "synthetic_secret" not in captured.out + captured.err
    assert "synthetic_person" not in captured.out + captured.err
    assert "PROTECTED FOUR-COHORT EXPORT BLOCKED" in captured.err
