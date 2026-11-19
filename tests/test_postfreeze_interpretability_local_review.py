"""Synthetic tests for private, create-only scientific interpretation report."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys

import pytest

from pcornet_omop_validation.study import (
    postfreeze_interpretability_local_review as review,
    postfreeze_model_interpretability_audit as audit,
)

REFERENCE = (
    Path(__file__).resolve().parent
    / "test_postfreeze_model_interpretability_audit.py"
)
spec = importlib.util.spec_from_file_location("synthetic_audit_fixtures", REFERENCE)
assert spec and spec.loader
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


def synthetic(tmp_path, monkeypatch):
    return fixture_module.synthetic_result(tmp_path, monkeypatch)


def test_review_covers_all_locked_comparisons_and_methods(
    tmp_path, monkeypatch,
):
    report = synthetic(tmp_path, monkeypatch)
    audit.audit(report)
    text = review.build_review(report)
    assert text.count("#### end_to_end") == 2
    assert text.count("#### representation_fixed") == 2
    assert text.count("#### representation_fallback") == 2
    assert text.count("#### selection_within_pcornet") == 2
    assert text.count("**random_forest**") == 8
    assert text.count("**xgboost**") == 8
    assert "RF/XGBoost delta-direction agreement" in text
    assert "Permutation AUROC decrease" in text
    assert "Mean absolute SHAP probability" in text
    assert "not" in text.lower()
    assert "RESTRICTED" in text
    assert "statistically significant based on permutation-repeat SD" in text


def test_restricted_review_file_has_0600_and_refuses_overwrite(
    tmp_path, monkeypatch,
):
    report = synthetic(tmp_path, monkeypatch)
    folder = tmp_path / "results" / "postfreeze_interpretability"
    folder.mkdir(parents=True, mode=0o700)
    folder.chmod(0o700)
    target = folder / "private_scientific_review.md"
    monkeypatch.setattr(review, "LOCAL_REVIEW", target)
    review.write_review(report)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    data = target.read_text(encoding="utf-8")
    assert "locked_six_features" in data
    assert "drop_date_sensitive_prior_stroke" in data
    assert "representation_fixed" in data
    with pytest.raises(audit.AuditBlocked, match="overwritten"):
        review.write_review(report)
    assert data == target.read_text(encoding="utf-8")


def test_scientific_review_blocks_unsafe_directory(tmp_path, monkeypatch):
    report = synthetic(tmp_path, monkeypatch)
    folder = tmp_path / "permissive"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    target = folder / "private_scientific_review.md"
    monkeypatch.setattr(review, "LOCAL_REVIEW", target)
    with pytest.raises(audit.AuditBlocked, match="unsafe"):
        review.write_review(report)
    assert not target.exists()


def test_cli_writes_private_report_without_printing_any_scores(
    tmp_path, monkeypatch, capsys,
):
    report = synthetic(tmp_path, monkeypatch)
    folder = tmp_path / "results" / "postfreeze_interpretability"
    folder.mkdir(parents=True, mode=0o700)
    folder.chmod(0o700)
    source = folder / "aggregate_results.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    source.chmod(0o600)
    target = folder / "private_scientific_review.md"
    monkeypatch.setattr(audit, "FILE", source)
    monkeypatch.setattr(review, "LOCAL_REVIEW", target)
    monkeypatch.setattr(sys, "argv", ["review"])
    review.main()
    visible = capsys.readouterr()
    assert "PRIVATE SCIENTIFIC REVIEW: WRITTEN" in visible.out
    assert "0.8000" not in visible.out
    assert "prior_365d" not in visible.out
    assert "end_to_end" not in visible.out
    assert target.is_file()
    with pytest.raises(SystemExit) as blocked:
        review.main()
    assert blocked.value.code == 1
    error = capsys.readouterr()
    assert "PRIVATE SCIENTIFIC REVIEW: BLOCKED" in error.err
    assert "0.8000" not in error.err
