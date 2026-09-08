from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fallback_sensitivity_is_locked_before_execution() -> None:
    path = ROOT / "study_definitions" / "stage_c_d_encounter_date_fallback_sensitivity_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["study_definition"] == "stage-c-d-encounter-date-fallback-sensitivity-v1"
    assert payload["status"] == (
        "reviewer_inspired_post_freeze_sensitivity_locked_before_fallback_execution"
    )
    assert payload["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert payload["relationship_to_completed_analyses"]["fallback_results"].startswith("Unknown")


def test_fallback_policy_and_nonconvergence_branches_are_explicit() -> None:
    payload = json.loads(
        (
            ROOT
            / "study_definitions"
            / "stage_c_d_encounter_date_fallback_sensitivity_v1.json"
        ).read_text(encoding="utf-8")
    )

    rules = payload["alternative_target_date_policy"]["date_rule"]
    assert rules[0]["date_basis"] == "recorded_diagnosis_date"
    assert rules[1]["date_basis"] == "encounter_admission_date_fallback"
    assert payload["guardrails"]["do_not_modify_frozen_target_database"] is True
    assert payload["guardrails"]["report_nonconvergence_if_observed"] is True
    assert "stage_c_residual_membership_discordance" in payload["precommitted_interpretation_branches"]
    assert "stage_d_nonconvergence" in payload["precommitted_interpretation_branches"]
