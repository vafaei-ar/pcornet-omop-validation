from __future__ import annotations

import ast
import json
from pathlib import Path


MODULE = Path("src/pcornet_omop_validation/study/manuscript_metadata_version_audit.py")


def test_metadata_audit_parses_and_is_read_only() -> None:
    text = MODULE.read_text(encoding="utf-8")
    ast.parse(text)
    lower = text.lower()
    assert "insert into" not in lower
    assert "update " not in lower
    assert "delete from" not in lower
    assert "drop table" not in lower
    assert "truncate" not in lower
    assert "patient_level_queries\": False" in text


def test_metadata_audit_separates_version_dimensions() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert "structural_cdm_release" in text
    assert "athena_vocabulary_snapshot" in text
    assert "source_pcornet_version" in text
    assert "Do not state an exact PCORnet source version" in text
    assert "vocabulary_version" in text
    assert "cdm_version" in text


def test_metadata_audit_anchors_frozen_etl() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    values = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "887e6f4d60a6b185e58b3c9fe8887472b49777e3" in values
