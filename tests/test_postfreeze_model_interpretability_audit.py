"""Synthetic audit tests for locked RF/XGBoost summaries; no private inputs."""
from __future__ import annotations

import hashlib
import json
import stat

import pytest

from pcornet_omop_validation.study import (
    postfreeze_model_interpretability as model,
    postfreeze_model_interpretability_audit as audit,
)


def synthetic_result(tmp_path, monkeypatch):
    study = tmp_path / "study.json"
    study.write_text('{"synthetic": true}', encoding="utf-8")
    monkeypatch.setattr(model, "STUDY_PATH", study)
    anchors = {}
    for group, (n, events) in model.EXPECTED.items():
        test_n = 1900 if n == 6508 else 1100
        anchors[group] = {
            "n": n, "events": events,
            "train_n": n - test_n, "test_n": test_n,
        }
    comparisons = {k: list(v[:2]) for k, v in audit.PAIRS.items()}
    obj = {
        "status": "postfreeze_interpretability_completed_pending_disclosure_review",
        "frozen_etl_sha": model.FROZEN_SHA,
        "study_definition_sha256": hashlib.sha256(study.read_bytes()).hexdigest(),
        "recorded_at_utc": "2026-11-01T00:00:00+00:00",
        "software_versions": {
            "python": "3.11", "numpy": "2", "pandas": "3",
            "scikit-learn": "1.9", "xgboost": "3.2", "shap": "0.51",
        },
        "permutation_repeats": 20,
        "importance_max_shap_evaluation_n": 500,
        "permutation_test_scope": "entire held-out population",
        "population_anchors": anchors,
        "comparisons": comparisons,
        "models": {},
        "feature_importance_comparisons": {},
    }

    def perf(n):
        return {
            "n": n, "events": 100, "prevalence": 100 / n,
            "auroc": 0.8, "auprc": 0.6, "brier": 0.12,
            "calibration_intercept": 0.01, "calibration_slope": 0.9,
        }

    def importance(n, features):
        vals = {
            f: {
                "permutation_auroc_decrease_mean": 0.01*(i+1),
                "permutation_auroc_decrease_sd": 0.001,
                "mean_absolute_shap_probability": 0.01*(i+1),
            }
            for i, f in enumerate(features)
        }
        return {
            "permutation_evaluation_n": n,
            "permutation_evaluation_events": 100,
            "shap_evaluation_n": 500,
            "shap_evaluation_events": 40,
            "explanation_scale": "positive-class predicted probability",
            "common_background_n": 200,
            "features": vals,
            "permutation_rank": list(reversed(features)),
            "shap_rank": list(reversed(features)),
        }

    for sensitivity, features in audit.SENSITIVITIES.items():
        obj["models"][sensitivity] = {}
        obj["feature_importance_comparisons"][sensitivity] = {}
        for algorithm in audit.MODELS:
            groups = {}
            for group in model.EXPECTED:
                native_n = anchors[group]["test_n"]
                groups[group] = {
                    "native_test_performance": perf(native_n),
                    "common_shared_test_performance": perf(1100),
                    "native_test_importance": importance(native_n, features),
                    "common_shared_test_importance": importance(1100, features),
                }
            obj["models"][sensitivity][algorithm] = groups
            obj["feature_importance_comparisons"][sensitivity][algorithm] = {}
            for name, (left, right, scope) in audit.PAIRS.items():
                a = groups[left][scope+"_importance"]
                b = groups[right][scope+"_importance"]
                obj["feature_importance_comparisons"][sensitivity][algorithm][name] = {
                    "left": left, "right": right, "evaluation_scope": scope,
                    **model.importance_difference(a, b),
                }
    return obj


def test_valid_matrix_and_contrasts_are_accepted(tmp_path, monkeypatch):
    report = synthetic_result(tmp_path, monkeypatch)
    audit.audit(report)


def test_wrong_rank_or_delta_is_blocked(tmp_path, monkeypatch):
    report = synthetic_result(tmp_path, monkeypatch)
    report["feature_importance_comparisons"]["locked_six_features"][
        "random_forest"]["end_to_end"]["feature_differences"][
            model.FEATURES[0]]["permutation_auroc_decrease_difference"] = 0.6
    with pytest.raises(audit.AuditBlocked, match="contrast difference"):
        audit.audit(report)


def test_wrong_permutation_denominator_is_blocked(tmp_path, monkeypatch):
    report = synthetic_result(tmp_path, monkeypatch)
    data = report["models"]["locked_six_features"]["random_forest"]["pcornet_full"]
    data["native_test_importance"]["permutation_evaluation_n"] = 500
    with pytest.raises(audit.AuditBlocked, match="permutation population"):
        audit.audit(report)


def test_wrong_explanation_background_is_blocked(tmp_path, monkeypatch):
    report = synthetic_result(tmp_path, monkeypatch)
    data = report["models"]["locked_six_features"]["xgboost"]["omop_frozen"]
    data["common_shared_test_importance"]["common_background_n"] = 199
    with pytest.raises(audit.AuditBlocked, match="background"):
        audit.audit(report)


def test_unmatched_common_events_are_blocked(tmp_path, monkeypatch):
    report = synthetic_result(tmp_path, monkeypatch)
    data = report["models"]["locked_six_features"]["random_forest"]["omop_frozen"]
    data["common_shared_test_performance"]["events"] = 101
    data["common_shared_test_performance"]["prevalence"] = 101/1100
    data["common_shared_test_importance"]["permutation_evaluation_events"] = 101
    with pytest.raises(audit.AuditBlocked, match="common held-out"):
        audit.audit(report)


def test_private_file_permissions_and_location(tmp_path, monkeypatch):
    root = tmp_path / "results" / "postfreeze_interpretability"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    filename = root / "aggregate_results.json"
    filename.write_text('{"synthetic":true}', encoding="utf-8")
    filename.chmod(0o600)
    monkeypatch.setattr(audit, "FILE", filename)
    audit.validate_private_path(filename)
    filename.chmod(0o644)
    with pytest.raises(audit.AuditBlocked, match="metadata"):
        audit.validate_private_path(filename)
    filename.chmod(0o600)
    with pytest.raises(audit.AuditBlocked, match="unexpected"):
        audit.validate_private_path(tmp_path / "other.json")


def test_terminal_only_reports_pass_or_blocked(tmp_path, monkeypatch, capsys):
    root = tmp_path / "results" / "postfreeze_interpretability"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    filename = root / "aggregate_results.json"
    report = synthetic_result(tmp_path, monkeypatch)
    filename.write_text(json.dumps(report), encoding="utf-8")
    filename.chmod(0o600)
    monkeypatch.setattr(audit, "FILE", filename)
    monkeypatch.setattr("sys.argv", ["audit"])
    audit.main()
    result = capsys.readouterr()
    assert "PRIVATE AGGREGATE AUDIT: PASS" in result.out
    assert "pcornet_full" not in result.out
    assert "0.8" not in result.out
    report["models"]["locked_six_features"]["random_forest"]["pcornet_full"][
        "native_test_performance"]["auroc"] = float("nan")
    filename.write_text(json.dumps(report), encoding="utf-8")
    filename.chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        audit.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "PRIVATE AGGREGATE AUDIT: BLOCKED" in output.err
    assert "nan" not in output.err
