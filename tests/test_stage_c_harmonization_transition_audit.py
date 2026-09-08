from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_harmonization_transition_definition_is_explicitly_post_result() -> None:
    path = ROOT / "study_definitions" / "stage_c_harmonization_transition_audit_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == (
        "post_result_descriptive_audit_defined_after_primary_and_harmonized_stage_c_results"
    )
    assert payload["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert payload["expected_count_anchors"]["omop_primary"]["D0"] == 6001
    assert payload["expected_count_anchors"]["omop_harmonized"]["D0"] == 6198


def test_harmonization_transition_audit_module_parses() -> None:
    path = (
        ROOT
        / "src"
        / "pcornet_omop_validation"
        / "study"
        / "stage_c_harmonization_transition_audit.py"
    )
    ast.parse(path.read_text(encoding="utf-8"))
