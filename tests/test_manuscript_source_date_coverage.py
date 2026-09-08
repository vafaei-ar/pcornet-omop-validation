from __future__ import annotations

import ast
from pathlib import Path


MODULE = Path("src/pcornet_omop_validation/study/manuscript_source_date_coverage.py")


def test_source_date_coverage_parses_and_is_read_only() -> None:
    text = MODULE.read_text(encoding="utf-8")
    ast.parse(text)
    lower = text.lower()
    assert "insert into" not in lower
    assert "update " not in lower
    assert "delete from" not in lower
    assert "drop table" not in lower
    assert "truncate" not in lower
    assert '"read_only": True' in text
    assert '"patient_identifiers_written": False' in text
    assert '"aggregate_dates_and_counts_only": True' in text


def test_source_date_coverage_uses_final_pcornet_source_tables() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert "PCORnet_ENCOUNTER" in text
    assert "PCORnet_DIAGNOSIS" in text
    assert "PCORnet_ENROLLMENT" in text
    assert "PCORnet_DEMOGRAPHIC" in text
    assert "ADMIT_DATE" in text
    assert "DISCHARGE_DATE" in text
    assert "DX_DATE" in text
    assert "ENR_START_DATE" in text
    assert "ENR_END_DATE" in text


def test_source_date_coverage_keeps_metadata_separate_from_locked_analysis() -> None:
    text = MODULE.read_text(encoding="utf-8")
    assert "FROZEN_ETL_SHA" in text
    assert "Do not reinterpret them as a new eligibility rule" in text
    assert "adult_ei_ip_overnight_coverage" in text
    assert "full_source_encounter_coverage" in text
