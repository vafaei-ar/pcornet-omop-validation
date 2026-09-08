from __future__ import annotations

import json
from pathlib import Path


PATH = Path("study_definitions/artifacts/stage_b_validation_scope_v1.json")


def _load() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_stage_b_scope_is_anchored_and_separates_validation_strength() -> None:
    d = _load()
    assert d["artifact"] == "stage-b-validation-scope-v1"
    assert d["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert set(d["scope_hierarchy"]) == {
        "internal_rule_consistency",
        "stronger_empirical_attribute_preservation",
    }


def test_route_consistency_is_not_called_independent_semantic_validation() -> None:
    d = _load()
    text = d["scope_hierarchy"]["internal_rule_consistency"]["interpretation"]
    assert "not an independent clinical or ontologic gold-standard validation" in text
    assert "should not be interpreted as independent semantic validation" in d["recommended_limitation_language"]


def test_stage_b_key_aggregates_are_locked() -> None:
    d = _load()
    internal = d["scope_hierarchy"]["internal_rule_consistency"]["results"]
    empirical = d["scope_hierarchy"]["stronger_empirical_attribute_preservation"]["results"]
    coverage = d["coverage_reported_separately"]
    assert internal["condition_mapped_routes_exact"] == 8983621
    assert internal["procedure_mapped_routes_exact"] == 11121561
    assert internal["drug_mapped_standard_routes_exact"] == 30988400
    assert internal["measurement_observation_mapped_rows_exact"] == 92668145
    assert empirical["numeric_directly_comparable"] == 75769622
    assert empirical["numeric_direct_exact"] == 75644000
    assert empirical["numeric_explained_vital_differences"] == 125622
    assert empirical["numeric_unexplained"] == 0
    assert empirical["uniquely_resolved_standard_ucum_rows"] == 58916347
    assert empirical["mapped_categorical_values"] == 809630
    assert coverage["condition_concept_zero_fallback"] == 60148
    assert coverage["procedure_unresolved_routes"] == 111660
    assert coverage["procedure_non_event_semantic_components"] == 1642
    assert coverage["drug_concept_zero_routes"] == 17469480
    assert coverage["measurement_observation_unresolved_or_descriptive_concept_zero"] == 366371


def test_stage_b_manuscript_guardrails_cover_circularity_and_vital_policy() -> None:
    d = _load()
    guardrails = " ".join(d["manuscript_guardrails"])
    assert "independent semantic validation" in guardrails
    assert "concept-zero and unresolved routes" in guardrails
    assert "vital-sign records" in guardrails
    assert "unexplained ETL error" in guardrails
