"""Synthetic Stage E metadata recovery validation tests (no institutional SQL)."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "recover_postfreeze_stage_e_preflight.py"
)
spec = importlib.util.spec_from_file_location("recover_stage_e", SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def setup_paths(tmp_path, monkeypatch):
    for key, attr in (
        ("study", "STUDY_PATH"),
        ("d0", "D0_PATH"),
        ("stage_d", "STAGE_D_PATH"),
    ):
        local = tmp_path / (key + ".json")
        local.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(module.original, attr, local)
    path = (
        tmp_path / "results" / "publication_analysis"
        / "stage_e_statistical_model"
        / "stage_e_statistical_model_preflight.json"
    )
    monkeypatch.setattr(module, "EXPECTED", path)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return path


def valid_payload():
    return {
        "status": "stage_e_statistical_model_preflight_ready",
        "frozen_etl_sha": module.FROZEN_SHA,
        "recorded_at_utc": "2026-10-31T12:00:00+00:00",
        "outcome_query_performed": False,
        "model_fit_performed": False,
        "analysis_worktree_clean": True,
        "feature_checks": {"test_feature": True},
        "study_definition_sha256": hashlib.sha256(
            module.original.STUDY_PATH.read_bytes()
        ).hexdigest(),
        "inherited_d0_definition_sha256": hashlib.sha256(
            module.original.D0_PATH.read_bytes()
        ).hexdigest(),
        "inherited_stage_d_definition_sha256": hashlib.sha256(
            module.original.STAGE_D_PATH.read_bytes()
        ).hexdigest(),
    }


def test_regenerated_metadata_explicitly_identifies_later_run(
    tmp_path, monkeypatch,
):
    setup_paths(tmp_path, monkeypatch)
    result = module.validate_regenerated(valid_payload())
    assert result["postfreeze_recovery_provenance"] == {
        "role": "regenerated_outcome_free_stage_e_metadata",
        "historical_original_file": "missing_from_local_results",
        "original_run_date_preserved": False,
        "frozen_database_modified": False,
        "restricted_qc_login": True,
        "date": "2026-10-31T12:00:00+00:00",
    }


@pytest.mark.parametrize("field,value", [
    ("outcome_query_performed", True),
    ("model_fit_performed", True),
    ("analysis_worktree_clean", False),
    ("study_definition_sha256", "wrong"),
    ("frozen_etl_sha", "wrong"),
])
def test_invalid_regenerated_metadata_blocked(
    tmp_path, monkeypatch, field, value,
):
    setup_paths(tmp_path, monkeypatch)
    data = valid_payload()
    data[field] = value
    with pytest.raises(module.AuditBlocked):
        module.validate_regenerated(data)


def test_existing_or_symlink_file_blocks_recovery_before_sql(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    path = setup_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module, "load_etl_config",
        lambda _: SimpleNamespace(audit_dir=Path("results/etl_audit")),
    )
    assert module.destination_for("synthetic.yaml") == path
    path.parent.mkdir(parents=True)
    path.write_text("original evidence", encoding="utf-8")
    with pytest.raises(module.AuditBlocked, match="already exists"):
        module.destination_for("synthetic.yaml")
    path.unlink()
    target = tmp_path / "another-file"
    target.write_text("original evidence")
    path.symlink_to(target)
    with pytest.raises(module.AuditBlocked):
        module.destination_for("synthetic.yaml")
