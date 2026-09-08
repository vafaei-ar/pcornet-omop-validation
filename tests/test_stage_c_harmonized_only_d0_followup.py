from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_harmonized_only_d0_followup_is_explicitly_post_result() -> None:
    path = ROOT / "study_definitions" / "stage_c_harmonized_only_d0_followup_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "post_result_descriptive_followup_defined_after_transition_audit"
    assert payload["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert payload["context"]["harmonized_only_source_d0"] == 1
    assert payload["report_regardless_of_direction"] is True


def test_harmonized_only_d0_followup_module_parses() -> None:
    path = (
        ROOT
        / "src"
        / "pcornet_omop_validation"
        / "study"
        / "stage_c_harmonized_only_d0_followup.py"
    )
    ast.parse(path.read_text(encoding="utf-8"))
