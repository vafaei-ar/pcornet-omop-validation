"""Offline synthetic validation for the Stage E aggregate-only recovery tool."""
import copy
import pytest
from scripts.qiran_stage_e_metric_recovery import recover

MODELS = ("logistic_regression", "ridge_logistic_regression", "hist_gradient_boosting")
TERMS = ("intercept", "age_at_index_years", "female_indicator",
         "index_length_of_stay_days", "prior_365d_acute_care_encounter_count",
         "prior_365d_all_encounter_count", "prior_365d_ischemic_stroke_indicator")


def make_payload():
    model_entry = {m: {"source": {"AUPRC": 0.31}, "omop": {"AUPRC": 0.32}}
                   for m in MODELS}
    association = {
        t: {
            "source": {"odds_ratio": 1.10, "ci95_lower": 1.01, "ci95_upper": 1.25},
            "omop": {"odds_ratio": 1.15, "ci95_lower": 1.02, "ci95_upper": 1.31},
            "odds_ratio_ratio_omop_over_source": 1.15 / 1.10,
        }
        for t in TERMS
    }
    return {
        "status": "stage_e_statistical_model_reproducibility_complete",
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
        "population_sizes": {"fixed": 3822, "source_end_to_end": 6508, "omop_end_to_end": 3822},
        "prediction_reproducibility": {"fixed_cohort": copy.deepcopy(model_entry),
                                        "end_to_end": copy.deepcopy(model_entry)},
        "association_reproducibility": {"fixed_cohort": copy.deepcopy(association),
                                        "end_to_end": copy.deepcopy(association)},
    }


def test_recover_synthetic_aggregates():
    output = recover(make_payload())
    assert output["status"] == "recovered_aggregate_pending_disclosure_review"
    assert output["AUPRC"]["fixed_cohort"]["logistic_regression"]["omop_minus_source"] == 0.01
    assert output["association_or_95ci"]["end_to_end"]["age_at_index_years"]["source"]["lower95"] == 1.01


def test_reject_wrong_frozen_anchor():
    payload = make_payload()
    payload["population_sizes"]["omop_end_to_end"] = 4100
    with pytest.raises(ValueError, match="anchors"):
        recover(payload)


def test_reject_wrong_etl():
    payload = make_payload()
    payload["frozen_etl_sha"] = "wrong"
    with pytest.raises(ValueError, match="SHA"):
        recover(payload)


def test_reject_incoherent_confidence_interval():
    payload = make_payload()
    payload["association_reproducibility"]["fixed_cohort"]["intercept"]["source"]["ci95_lower"] = 1.20
    with pytest.raises(ValueError, match="outside"):
        recover(payload)
