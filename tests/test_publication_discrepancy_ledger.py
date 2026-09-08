from __future__ import annotations

import json
from pathlib import Path


PATH = Path("study_definitions/artifacts/publication_discrepancy_ledger_v1.json")


def _load() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_discrepancy_ledger_taxonomy_and_freeze_anchor() -> None:
    d = _load()
    assert d["artifact"] == "publication-discrepancy-ledger-v1"
    assert d["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert set(d["taxonomy"]) == {
        "etl_defect",
        "etl_policy_effect",
        "cdm_representation_limitation",
        "vocabulary_limitation",
        "analysis_definition_difference",
    }


def test_primary_stage_c_and_stage_d_mechanisms_are_not_called_etl_defects() -> None:
    d = _load()
    by_id = {row["id"]: row for row in d["ledger"]}
    assert by_id["primary_stage_c_diagnosis_date_cohort_discordance"]["classification"] == "etl_policy_effect"
    assert by_id["stage_d_end_to_end_risk_shift_from_selective_cohort_loss"]["classification"] == "etl_policy_effect"
    assert by_id["historical_pre_freeze_conversion_implementation_issues"]["classification"] == "etl_defect"
    assert by_id["historical_pre_freeze_conversion_implementation_issues"]["status_in_frozen_etl"] == "corrected_before_freeze"


def test_stage_b_scope_guardrails_are_explicit() -> None:
    d = _load()
    notes = d["validation_scope_notes"]
    assert "not be presented as independent semantic validation" in notes["stage_b_route_concordance"]
    assert "independent clinical gold standard" in notes["gold_standard_limitation"]


def test_selective_loss_guardrail_avoids_missingness_and_causal_overclaim() -> None:
    d = _load()
    by_id = {row["id"]: row for row in d["ledger"]}
    guardrail = by_id["stage_d_end_to_end_risk_shift_from_selective_cohort_loss"]["manuscript_guardrail"]
    assert "MCAR/MAR/MNAR" in guardrail
    assert "formal causal selection effect" in guardrail
