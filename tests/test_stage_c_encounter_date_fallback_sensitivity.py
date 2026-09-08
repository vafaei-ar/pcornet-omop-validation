from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "study_definitions" / "stage_c_d_encounter_date_fallback_sensitivity_v1.json"
MODULE = (
    ROOT
    / "src"
    / "pcornet_omop_validation"
    / "study"
    / "stage_c_encounter_date_fallback_sensitivity.py"
)


def test_fallback_lock_precedes_execution_and_has_unknown_target_counts() -> None:
    payload = json.loads(LOCK.read_text(encoding="utf-8"))
    assert payload["status"] == (
        "reviewer_inspired_post_freeze_sensitivity_locked_before_fallback_execution"
    )
    assert payload["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert payload["stage_c_analysis"]["known_source_anchor_counts"] == {
        "D0": 9815,
        "D1": 8624,
        "D3": 7565,
    }
    assert payload["stage_c_analysis"]["fallback_target_expected_counts"] == (
        "Not prespecified. The analysis must report observed values even if they do not converge to the source counts."
    )


def test_fallback_module_parses_and_is_non_destructive() -> None:
    source = MODULE.read_text(encoding="utf-8")
    ast.parse(source)
    upper = source.upper()
    # Session-local SELECT INTO / temp-table DDL are allowed; writes to frozen tables are not.
    assert "INSERT INTO" not in upper
    assert "UPDATE [" not in upper
    assert "DELETE FROM" not in upper
    assert "#FB_TARGET_BASE" in upper
    assert "ENCOUNTER_ADMISSION_DATE_FALLBACK" in upper
    assert "RECORDED_DIAGNOSIS_DATE" in upper


def test_fallback_module_does_not_hard_code_target_result_anchors() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert "EXPECTED_SOURCE" in source
    assert "EXPECTED_TARGET" not in source
    assert "fallback_target_expected_counts" not in source
