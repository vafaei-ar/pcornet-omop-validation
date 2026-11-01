"""Synthetic checks for protected RF/XGBoost robustness without clinical data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

from pcornet_omop_validation.study import (
    postfreeze_interpretability_stability as stability,
    postfreeze_model_interpretability as m,
    postfreeze_model_interpretability_audit as audit,
)


def cohorts(n=600):
    ids = [f"synthetic-{i:04d}" for i in range(n)]
    frame = pd.DataFrame({
        "patid": ids,
        "index_date": ["2024-01-01"] * n,
        "outcome": [i % 2 for i in range(n)],
        **{
            name: (
                [float(i % 2) for i in range(n)]
                if name in m.BINARY
                else [float(i % 11) for i in range(n)]
            )
            for name in m.FEATURES
        },
    }).set_index("patid", drop=False)
    frame["is_train"] = frame.patid.map(m.is_train)
    shared = frame.iloc[:n//2].copy()
    return {
        "pcornet_full": frame,
        "pcornet_shared": shared,
        "omop_frozen": shared.copy(),
        "omop_fallback": frame.copy(),
    }


def synthetic_audited_result(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_postfreeze_model_interpretability_audit_fixture",
        Path(__file__).resolve().parent /
        "test_postfreeze_model_interpretability_audit.py",
    )
    assert spec and spec.loader
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    return source.synthetic_result(tmp_path, monkeypatch)


def fake_trials():
    outputs = {}
    for sensitivity, features in audit.SENSITIVITIES.items():
        outputs[sensitivity] = {}
        for algorithm in audit.MODELS:
            outputs[sensitivity][algorithm] = {}
            for group in m.EXPECTED:
                outputs[sensitivity][algorithm][group] = [
                    {
                        scope: {
                            name: float(0.01 * (idx + 1) + 0.001 * trial)
                            for idx, name in enumerate(features)
                        }
                        for scope in ("native_test", "common_shared_test")
                    }
                    for trial in range(stability.REPLICATES)
                ]
    return outputs


def test_training_perturbations_keep_matched_patient_inclusion():
    populations = cohorts()
    a = populations["pcornet_shared"]
    b = populations["omop_frozen"]
    for rep in range(stability.REPLICATES):
        x = stability.training_subset(a, rep)
        y = stability.training_subset(b, rep)
        assert x.index.equals(y.index)
        assert x.index.equals(stability.training_subset(a, rep).index)
        assert x.outcome.nunique() == 2
    assert len(stability.training_subset(a, 0)) == int(a.is_train.sum())
    assert any(
        len(stability.training_subset(a, rep)) < int(a.is_train.sum())
        for rep in range(1,stability.REPLICATES)
    )


def test_feature_diagnostics_detect_correlations_and_paired_disagreement():
    groups = cohorts()
    left = groups["pcornet_shared"]
    right = groups["omop_frozen"]
    right.loc[right.index[0], "prior_365d_ischemic_stroke_indicator"] = (
        1 - right.iloc[0]["prior_365d_ischemic_stroke_indicator"]
    )
    lines = "\n".join(stability.predictor_diagnostics(groups))
    assert "| Spearman rho |" in lines
    assert "Yes" in lines  # multiple synthetic predictors are perfectly related
    assert "pcornet_shared vs omop_frozen" in lines
    assert "prior_365d_ischemic_stroke_indicator" in lines
    assert "Matched population pair" in lines


def test_perturbed_models_run_separately_without_row_level_export(monkeypatch):
    groups = cohorts()
    observed = []
    class FakeModel:
        def set_params(self, **kwargs):
            self.seed = kwargs["random_state"]
            return self
    def make(name):
        observed.append(("model",name))
        return FakeModel()
    def probe(model, train, test, features):
        observed.append(("fit", model.seed, len(train), len(test["native_test"])))
        return {
            scope: {f: float(i+1)*.01 for i,f in enumerate(features)}
            for scope in test
        }
    monkeypatch.setattr(stability.m, "make_model", make)
    monkeypatch.setattr(stability, "_model_permutation", probe)
    result = stability.compute_seed_stability(groups)
    assert set(result) == set(audit.SENSITIVITIES)
    assert len(result["locked_six_features"]["random_forest"]["pcornet_full"]) == 5
    assert sum(event[0] == "fit" for event in observed) == 80
    assert sum(event[0] == "model" for event in observed) == 80
    assert all(set(event) == {"native_test","common_shared_test"}
               for sens in result.values() for algo in sens.values()
               for group in algo.values() for event in group)


def test_private_scientific_stability_report_contains_all_contrasts(
    tmp_path, monkeypatch,
):
    old = synthetic_audited_result(tmp_path, monkeypatch)
    txt = "\n".join(stability.render_stability(fake_trials(), old))
    assert txt.count("#### end_to_end") == 2
    assert txt.count("#### representation_fixed") == 2
    assert txt.count("#### representation_fallback") == 2
    assert txt.count("#### selection_within_pcornet") == 2
    assert "positive / zero / negative".lower() in txt.lower()
    assert "not a reproduction" in txt
    assert "confidence intervals" in txt
    assert "patient-level" in txt


def test_private_cli_suppresses_protected_diagnostics(monkeypatch, capsys):
    def fail(_):
        raise RuntimeError("PATID=synthetic_private_marker")
    monkeypatch.setattr(stability.audit,"validate_private_path",fail)
    with pytest.raises(SystemExit) as ex:
        stability.main()
    assert ex.value.code == 1
    output = capsys.readouterr()
    assert "PRIVATE STABILITY REVIEW: BLOCKED" in output.err
    assert "synthetic_private_marker" not in output.err + output.out


def test_full_private_stability_output_is_create_only_0600(
    tmp_path, monkeypatch, capsys,
):
    root = tmp_path / "results" / "postfreeze_interpretability"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    # Use a schema-complete synthetic model report. The report builder
    # reads baseline feature ranks, so an unrelated placeholder JSON
    # cannot exercise the successful create-only output path.
    prior = synthetic_audited_result(tmp_path, monkeypatch)
    source = root / "aggregate_results.json"
    source.write_text(json.dumps(prior), encoding="utf-8")
    source.chmod(0o600)
    target = root / "private_stability_review.md"
    monkeypatch.setattr(stability,"DESTINATION",target)
    monkeypatch.setattr(stability.audit,"FILE",source)
    monkeypatch.setattr(stability.audit,"validate_private_path",lambda _:None)
    monkeypatch.setattr(stability.audit,"audit",lambda _:None)
    data=cohorts()
    monkeypatch.setattr(stability.m,"read_population",
                        lambda path, group: data[group])
    monkeypatch.setattr(stability.m,"validate_populations",lambda _:None)
    monkeypatch.setattr(stability,"compute_seed_stability",
                        lambda groups:fake_trials())
    monkeypatch.setattr(stability,"predictor_diagnostics",
                        lambda groups:["synthetic-only private diagnostics"])
    stability.main()
    visible=capsys.readouterr()
    assert "PRIVATE STABILITY REVIEW: COMPLETE" in visible.out
    assert "synthetic-only private diagnostics" not in visible.out
    assert stat.S_IMODE(target.stat().st_mode)==0o600
    assert "synthetic-only private diagnostics" in target.read_text()
    with pytest.raises(SystemExit):
        stability.main()
    assert "PRIVATE STABILITY REVIEW: BLOCKED" in capsys.readouterr().err
