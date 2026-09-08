from __future__ import annotations

import json
from pathlib import Path


PATH = Path("study_definitions/artifacts/stage_d_uncertainty_tolerance_interpretation_v1.json")


def _load() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_stage_d_tolerances_match_locked_definition() -> None:
    d = _load()
    p = d["prespecification"]
    assert d["artifact"] == "stage-d-uncertainty-tolerance-interpretation-v1"
    assert d["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert p["absolute_risk_difference_tolerance_percentage_points"] == 0.5
    assert p["relative_risk_ratio_lower"] == 0.95
    assert p["relative_risk_ratio_upper"] == 1.05
    assert p["margins_locked_before_outcomes"] is True
    assert p["post_result_margin_changes_prohibited"] is True


def test_tolerances_are_not_mischaracterized_as_clinical_equivalence() -> None:
    d = _load()
    i = d["interpretation"]
    assert i["preferred_term"] == "empirical cross-CDM reproducibility tolerances"
    assert i["not_clinical_equivalence_margin"] is True
    assert i["not_noninferiority_margin"] is True
    assert i["not_minimal_clinically_important_difference"] is True
    assert i["not_formal_ci_based_equivalence_test"] is True


def test_uncertainty_decision_preserves_deterministic_validation_estimand() -> None:
    d = _load()
    u = d["uncertainty_decision"]
    assert u["bootstrap_required_for_primary_claim"] is False
    assert u["confidence_intervals_required_for_tolerance_decision"] is False
    assert "same underlying data resource" in u["rationale"][0]
    assert "patient by patient" in u["rationale"][1]
    assert "dependence between representations" in u["rationale"][2]
    assert "different question" in u["rationale"][3]


def test_manuscript_guardrails_forbid_posthoc_inferential_redefinition() -> None:
    d = _load()
    g = " ".join(d["manuscript_guardrails"])
    assert "Do not retrofit confidence intervals" in g
    assert "preserve patient-level dependence across representations" in g
    assert "Do not claim general PCORnet-OMOP equivalence" in g
