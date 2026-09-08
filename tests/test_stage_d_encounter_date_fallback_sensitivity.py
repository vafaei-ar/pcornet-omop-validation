from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stage_d_fallback_definition_was_locked_before_execution() -> None:
    path = ROOT / "study_definitions" / "stage_c_d_encounter_date_fallback_sensitivity_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == (
        "reviewer_inspired_post_freeze_sensitivity_locked_before_fallback_execution"
    )
    assert payload["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert payload["stage_d_analysis"]["known_source_anchors"]["30_day"] == {
        "eligible": 7277,
        "events": 1178,
    }
    assert payload["stage_d_analysis"]["known_source_anchors"]["90_day"] == {
        "eligible": 6508,
        "events": 1798,
    }
    assert payload["guardrails"]["do_not_modify_frozen_target_database"] is True
    assert payload["guardrails"]["do_not_change_stage_d_outcome_rules_or_tolerances"] is True


def test_stage_d_fallback_module_parses_and_is_non_destructive() -> None:
    path = (
        ROOT
        / "src"
        / "pcornet_omop_validation"
        / "study"
        / "stage_d_encounter_date_fallback_sensitivity.py"
    )
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    upper = source.upper()
    assert "INSERT INTO [" not in upper
    assert "UPDATE [" not in upper
    assert "DELETE FROM [" not in upper
    assert "DROP TABLE [DBO]" not in upper
    assert "STAGE_C_RESULT_PATH" in source
    assert "EXPECTED_SOURCE_OUTCOME" in source
    assert "jointly_observable_label_check" in source
