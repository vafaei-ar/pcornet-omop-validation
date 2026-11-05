from __future__ import annotations

import hashlib

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

from pcornet_omop_validation.study import postfreeze_model_interpretability as m


def make_frame(ids, index_shift=False):
    frame = pd.DataFrame({
        "patid": list(ids),
        "index_date": ["2024-01-01" if not index_shift else "2024-01-02"] * len(ids),
        "outcome": [i % 2 for i in range(len(ids))],
        **{feature: [float(i % 7) for i in range(len(ids))] for feature in m.FEATURES},
    }).set_index("patid", drop=False)
    frame["is_train"] = frame.patid.map(m.is_train)
    return frame


def test_split_identical_to_locked_sha256_rule():
    for value in ["1", "patient-1", "example", "hello"]:
        digest = hashlib.sha256(value.encode()).digest()
        assert m.is_train(value) == (int.from_bytes(digest[:8], "big") % 10 < 7)


def test_matching_pairs_and_index_dates():
    a = make_frame(range(20))
    b = a.copy()
    m.assert_pair(a, b, "same")
    b.loc[b.index[0], "index_date"] = "2024-01-02"
    with pytest.raises(ValueError, match="index date mismatch"):
        m.assert_pair(a, b, "not same")


def test_four_population_anchor_protection():
    a = make_frame(range(200))
    shared = a.iloc[:100].copy()
    target = shared.copy()
    fallback = a.copy()
    groups = dict(pcornet_full=a, pcornet_shared=shared,
                  omop_frozen=target, omop_fallback=fallback)
    m.validate_populations(groups)
    target.loc[target.index[0], "outcome"] = 1 - int(target.iloc[0].outcome)
    with pytest.raises(ValueError, match="outcome mismatch"):
        m.validate_populations(groups)


def test_date_sensitive_feature_exclusion_is_exact():
    less = [f for f in m.FEATURES if f != "prior_365d_ischemic_stroke_indicator"]
    assert len(m.FEATURES) == 6
    assert len(less) == 5
    assert "prior_365d_ischemic_stroke_indicator" not in less





def test_common_shap_background_uses_reproducible_random_training_sample():
    frame = make_frame(range(600))
    training = frame[frame.is_train]
    expected = training.sort_index().sample(n=200, random_state=m.SEED)
    first = m.common_shap_background(frame)
    repeated = m.common_shap_background(frame)
    reordered = m.common_shap_background(
        frame.sample(frac=1, random_state=9)
    )
    assert len(first) == 200
    assert first.index.equals(expected.index)
    assert first.index.equals(repeated.index)
    assert first.index.equals(reordered.index)
    assert set(first.index).issubset(set(training.index))
    assert not first.index.equals(training.sort_index().head(200).index)
    assert len(m.common_shap_background(frame, limit=30)) == 30
    with pytest.raises(ValueError, match="positive"):
        m.common_shap_background(frame, limit=0)


def test_models_expose_probability_and_positive_shap_shape():
    pytest.importorskip("sklearn")
    pytest.importorskip("xgboost")
    pytest.importorskip("shap")
    rng = np.random.default_rng(25)
    x = pd.DataFrame(rng.normal(size=(120, 6)), columns=m.FEATURES)
    y = ((x[m.FEATURES[0]] + x[m.FEATURES[2]]) > 0).astype(int)
    train = pd.DataFrame(x.iloc[:90])
    train["outcome"] = y.iloc[:90]
    test = pd.DataFrame(x.iloc[90:])
    test["outcome"] = y.iloc[90:]
    background = train.iloc[:30].copy()
    for name in ("random_forest", "xgboost"):
        model = m.make_model(name)
        model.set_params(n_estimators=12)
        model.fit(train[m.FEATURES], train.outcome)
        importance = m.attribution(
            model, m.FEATURES, test, background,
            {f: 0.0 for f in m.FEATURES}, repeats=2,
        )
        assert set(importance["features"]) == set(m.FEATURES)
        assert importance["permutation_evaluation_n"] == 30
        assert importance["shap_evaluation_n"] == 30
        assert sorted(importance["shap_rank"]) == sorted(m.FEATURES)



def test_model_private_input_and_aggregate_output_guards(tmp_path, monkeypatch):
    import json
    import stat

    inp = tmp_path / "results" / "private_inputs"
    inp.mkdir(parents=True, mode=0o700)
    inp.chmod(0o700)
    out = tmp_path / "results" / "postfreeze_interpretability"
    monkeypatch.setattr(m, "PRIVATE_INPUT_ROOT", inp)
    monkeypatch.setattr(m, "PRIVATE_RESULTS_ROOT", out)

    cohort = inp / "pcornet_full.parquet"
    cohort.write_bytes(b"synthetic-only")
    cohort.chmod(0o600)
    assert m.validate_private_input_path(str(cohort), "pcornet_full") == cohort
    with pytest.raises(ValueError, match="matching"):
        m.validate_private_input_path(str(cohort), "omop_frozen")
    cohort.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        m.validate_private_input_path(str(cohort), "pcornet_full")
    cohort.chmod(0o600)
    real = inp / "real.parquet"
    cohort.rename(real)
    cohort.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        m.validate_private_input_path(str(cohort), "pcornet_full")

    target = out / "aggregate_results.json"
    assert m.validate_private_output_path(str(target)) == target
    m.write_private_aggregate(target, {"status": "synthetic-only"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert json.loads(target.read_text())["status"] == "synthetic-only"
    with pytest.raises(FileExistsError):
        m.write_private_aggregate(target, {"status": "must-not-overwrite"})


def test_model_private_output_must_be_inside_locked_results(tmp_path, monkeypatch):
    out = tmp_path / "results" / "postfreeze_interpretability"
    monkeypatch.setattr(m, "PRIVATE_RESULTS_ROOT", out)
    with pytest.raises(ValueError, match="escapes"):
        m.validate_private_output_path(str(tmp_path / "other" / "aggregate_results.json"))
    with pytest.raises(ValueError, match="locked aggregate"):
        m.validate_private_output_path(str(out / "other.json"))


def test_importance_difference_reports_right_minus_left():
    feats = m.FEATURES[:2]
    def report(a, b):
        return {
            "features": {
                feats[0]: {"permutation_auroc_decrease_mean": a,
                           "mean_absolute_shap_probability": b},
                feats[1]: {"permutation_auroc_decrease_mean": b,
                           "mean_absolute_shap_probability": a},
            },
            "permutation_rank": feats if a > b else feats[::-1],
            "shap_rank": feats if b > a else feats[::-1],
            "permutation_evaluation_n": 123,
        }
    difference = m.importance_difference(report(.1, .2), report(.3, .4))
    item = difference["feature_differences"][feats[0]]
    assert item["permutation_auroc_decrease_difference"] == pytest.approx(.2)
    assert item["mean_absolute_shap_probability_difference"] == pytest.approx(.2)
    assert difference["left_permutation_evaluation_n"] == 123


def test_analysis_uses_full_heldout_permutation_and_capped_matched_shap(
    monkeypatch,
):
    full = make_frame(range(500))
    shared = full.iloc[:250].copy()
    groups = {
        "pcornet_full": full,
        "pcornet_shared": shared,
        "omop_frozen": shared.copy(),
        "omop_fallback": full.copy(),
    }
    observations = []
    class FakeModel:
        def fit(self, x, y):
            return self
        def predict_proba(self, x):
            return np.column_stack((np.full(len(x), .7),
                                    np.full(len(x), .3)))

    def fake_importance(model, features, test, background, impute, repeats,
                        shap_sample=None):
        observations.append((len(test), len(shap_sample), repeats))
        return {
            "permutation_evaluation_n": len(test),
            "features": {
                f: {"permutation_auroc_decrease_mean": .1,
                    "mean_absolute_shap_probability": .2}
                for f in features
            },
            "permutation_rank": list(features),
            "shap_rank": list(features),
        }

    monkeypatch.setattr(m, "make_model", lambda name: FakeModel())
    monkeypatch.setattr(m, "attribution", fake_importance)
    monkeypatch.setattr(m, "performance", lambda y, p: {"n": len(y)})
    result = m.analyze(groups, max_eval=50, repeats=2)
    assert len(observations) == 32
    common_n = int((~shared.is_train).sum())
    assert common_n > 50
    assert all(n >= sample and sample <= 50 and repeats == 2
               for n, sample, repeats in observations)
    assert sum(n == common_n for n, sample, _ in observations) >= 16
    expected = {"end_to_end", "selection_within_pcornet",
                "representation_fixed", "representation_fallback"}
    assert set(result["comparisons"]) == expected
    for sensitivity in result["feature_importance_comparisons"].values():
        for model_data in sensitivity.values():
            assert set(model_data) == expected



def test_model_validate_only_checks_cohorts_without_fitting_or_writing(
    tmp_path, monkeypatch, capsys,
):
    import json
    import sys

    study = tmp_path / "locked-protocol.json"
    study.write_text(json.dumps({
        "status": "proposed_postfreeze_protocol_not_executed",
        "frozen_etl_sha": m.FROZEN_SHA,
        "interpretability": {
            "features": m.FEATURES,
            "populations": [
                {"key": key, "expected_n": value[0]}
                for key, value in m.EXPECTED.items()
            ],
        },
    }), encoding="utf-8")
    monkeypatch.setattr(m, "STUDY_PATH", study)
    full = make_frame(range(450))
    shared = full.iloc[:225].copy()
    groups = dict(
        pcornet_full=full,
        pcornet_shared=shared,
        omop_frozen=shared.copy(),
        omop_fallback=full.copy(),
    )
    monkeypatch.setattr(m, "read_population",
                        lambda file, name: groups[name])
    monkeypatch.setattr(m, "validate_private_output_path",
                        lambda _: tmp_path / "not-created.json")
    def must_not_fit(*args, **kwargs):
        raise AssertionError("Model fitting must not happen in validate-only")
    monkeypatch.setattr(m, "analyze", must_not_fit)
    monkeypatch.setattr(m, "write_private_aggregate", must_not_fit)
    command = ["model", "--validate-only"]
    for key in m.EXPECTED:
        command += ["--" + key.replace("_", "-"), "synthetic"]
    monkeypatch.setattr(sys, "argv", command)
    m.main()
    output = capsys.readouterr().out
    assert "PRIVATE INPUT VALIDATION: PASS" in output
    assert "No model fitted, no files written" in output
    assert not (tmp_path / "not-created.json").exists()


def test_model_cli_hides_invalid_private_data_diagnostics(
    tmp_path, monkeypatch, capsys,
):
    import json
    import sys

    study = tmp_path / "locked-protocol.json"
    study.write_text(json.dumps({
        "status": "proposed_postfreeze_protocol_not_executed",
        "frozen_etl_sha": m.FROZEN_SHA,
        "interpretability": {
            "features": m.FEATURES,
            "populations": [
                {"key": key, "expected_n": value[0]}
                for key, value in m.EXPECTED.items()
            ],
        },
    }), encoding="utf-8")
    monkeypatch.setattr(m, "STUDY_PATH", study)
    monkeypatch.setattr(m, "validate_private_output_path",
                        lambda _: tmp_path / "not-created.json")
    private_text = "PATID=synthetic_identifier"
    def fail_read(*args):
        raise RuntimeError(private_text)
    monkeypatch.setattr(m, "read_population", fail_read)
    command = ["model", "--validate-only"]
    for key in m.EXPECTED:
        command += ["--" + key.replace("_", "-"), "synthetic"]
    monkeypatch.setattr(sys, "argv", command)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 1
    visible = capsys.readouterr()
    assert private_text not in visible.out + visible.err
    assert "PRIVATE ANALYSIS BLOCKED" in visible.err
