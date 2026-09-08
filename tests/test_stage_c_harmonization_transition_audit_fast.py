from __future__ import annotations

import ast
from pathlib import Path


def test_stage_c_harmonization_transition_audit_fast_parses() -> None:
    path = Path(
        "src/pcornet_omop_validation/study/"
        "stage_c_harmonization_transition_audit_fast.py"
    )
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    assert "set_based_evidence_materialization_v2" in source
    assert "EXPECTED_TARGET_HARMONIZED" in source
    assert "frozen lineage" in source.lower()
