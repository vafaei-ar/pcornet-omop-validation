from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "study_definitions" / "stage_d_source_only_complement_audit_v1.json"
SCRIPT = (
    ROOT
    / "src"
    / "pcornet_omop_validation"
    / "study"
    / "stage_d_source_only_complement_audit.py"
)


def test_source_only_complement_audit_is_explicitly_post_outcome() -> None:
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    assert definition["status"] == "post_outcome_audit_defined_after_primary_stage_d_results"
    assert definition["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert definition["timing_and_role"]["primary_stage_d_results_known_before_definition"] is True
    assert definition["timing_and_role"]["does_not_redefine_primary_estimand"] is True
    assert definition["reporting_commitment"]["report_regardless_of_direction_or_magnitude"] is True
    assert definition["privacy"]["patient_level_outputs_committed_to_git"] is False


def test_source_only_complement_audit_script_parses() -> None:
    ast.parse(SCRIPT.read_text(encoding="utf-8"))
