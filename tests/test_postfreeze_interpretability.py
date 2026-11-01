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
        assert importance["evaluation_n"] == 30
        assert sorted(importance["shap_rank"]) == sorted(m.FEATURES)
