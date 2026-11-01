"""Synthetic tests for read-only analysis-210 overlap with locked Stage C/D D0.

No institutional database, credentials or row-level data.
"""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
import sys

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "audit_postfreeze_achilles_210_cohort_overlap.py"
)
SPEC = importlib.util.spec_from_file_location("postfreeze_210_overlap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_locked_sql_mirrors_original_d0_and_210_is_select_only():
    sql = " ".join(module.overlap_sql().split()).lower()
    assert sql.startswith(";with dx_rank as (")
    assert "pcornet_diagnosis" in sql
    assert "pcornet_encounter" in sql
    assert "pcornet_demographic" in sql
    assert "dx_date is null then 1" in sql
    assert "order by index_date,encounterid" in sql
    assert "datediff(day,birth_date,index_date)/365.0" in sql
    assert "cx.source_domain='diagnosis'" in sql
    assert "co.visit_occurrence_id=v.visit_occurrence_id" in sql
    assert "order by omop_index_date,condition_occurrence_id" in sql
    assert "vo.visit_start_date>=op.observation_period_start_date" in sql
    assert "vo.visit_start_date<=op.observation_period_end_date" in sql
    assert "where not exists" in sql
    assert "visit_concept_id in (9203,262,9201)" in sql
    assert "dateadd(day,30,o.omop_discharge_date)" in sql
    assert "dateadd(day,90,o.omop_discharge_date)" in sql
    for forbidden in ("select into", " into #", "update [", "insert into",
                      "create table", "drop table", "delete from",
                      "merge into", "truncate table", "execute "):
        assert forbidden not in sql


def _valid_columns():
    return {
        "source_d0_patients": 9815,
        "omop_lineage_d0_patients": 6001,
        "exception_visits": 2,
        "exception_visits_on_source_d0_patients": 1,
        "exception_visits_on_omop_d0_patients": 1,
        "exception_index_visits_on_omop_d0": 0,
        "exception_acute_visits_30d_on_omop_d0": 0,
        "exception_acute_visits_90d_on_omop_d0": 1,
        "exception_omop_d0_patients": 1,
    }


class SyntheticCursor:
    def __init__(self, row):
        self.row = row
        self.calls = 0
        self.closed = False

    def execute(self, sql):
        assert sql.strip().lower().startswith(";with dx_rank as (")
        assert "select into" not in sql.lower()
        return self

    def fetchone(self):
        self.calls += 1
        return self.row if self.calls == 1 else None

    def close(self):
        self.closed = True


class SyntheticConnection:
    def __init__(self, mapping=None):
        self.mapping = _valid_columns() if mapping is None else mapping
        self.cursor_instance = SyntheticCursor(
            tuple(self.mapping[col] for col in module.COLUMNS)
        )

    def cursor(self):
        return self.cursor_instance


def test_sql_aggregate_validation_accepts_locked_anchor_and_suppression(monkeypatch):
    monkeypatch.setattr(module, "verify_identity", lambda *a: None)
    conn = SyntheticConnection()
    result = module.query_overlap(conn, expected=2)
    assert result == _valid_columns()
    assert conn.cursor_instance.closed


@pytest.mark.parametrize("col,new_value,fragment", [
    ("source_d0_patients", 9814, "cohort anchors"),
    ("omop_lineage_d0_patients", 6002, "cohort anchors"),
    ("exception_visits", 3, "count mismatch"),
    ("exception_visits_on_omop_d0_patients", 3, "exceeds total"),
    ("exception_acute_visits_30d_on_omop_d0", 2, "consistency"),
    ("exception_omop_d0_patients", 2, "consistency"),
])
def test_sql_aggregate_validation_blocks_mismatch(monkeypatch, col, new_value, fragment):
    monkeypatch.setattr(module, "verify_identity", lambda *a: None)
    data = _valid_columns()
    data[col] = new_value
    with pytest.raises(module.AuditBlocked, match=fragment):
        module.query_overlap(SyntheticConnection(data), expected=2)


def test_existing_private_210_is_validated_before_read(tmp_path, monkeypatch):
    p = tmp_path / "four_analysis_exact_counts.json"
    p.write_text(json.dumps({"analyses": {"210": {"exception_count": 2}}}),
                 encoding="utf-8")
    monkeypatch.setattr(module, "PRIVATE_OUTPUT", p)
    verified = []
    monkeypatch.setattr(module, "verify_existing_private_output",
                        lambda: verified.append(True))
    assert module.validate_private_metadata() == 2
    assert verified == [True]
    p.write_text(json.dumps({"analyses": {"210": {"exception_count": 11}}}),
                 encoding="utf-8")
    with pytest.raises(module.AuditBlocked, match="Unexpected private"):
        module.validate_private_metadata()


def _private_path(tmp_path, monkeypatch):
    base = (
        tmp_path / "results" / "postfreeze_standard_qc"
        / "private_achilles_exception_counts"
    )
    base.mkdir(parents=True, mode=0o700)
    base.chmod(0o700)
    destination = base / "analysis_210_locked_d0_overlap.json"
    monkeypatch.setattr(module, "OUTPUT", destination)
    monkeypatch.setattr(module, "PRIVATE_DIR", base)
    return destination


def test_save_private_review_is_0600_and_never_overwrites(tmp_path, monkeypatch):
    p = _private_path(tmp_path, monkeypatch)
    module.save_private_review(_valid_columns(), 2)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["cohort_overlap"]["exception_visits"] == 2
    assert data["cohort_sql_performed"] is True
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    with pytest.raises(module.AuditBlocked, match="overwritten"):
        module.save_private_review(_valid_columns(), 2)
    assert json.loads(p.read_text(encoding="utf-8")) == data


def test_zero_exception_shortcut_is_private_and_requires_no_sql(
    tmp_path, monkeypatch, capsys,
):
    p = _private_path(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "verify_manifest", lambda: None)
    monkeypatch.setattr(module, "validate_private_metadata", lambda: 0)
    monkeypatch.setattr(module, "verified_server_config",
                        lambda *a: pytest.fail("No SQL config needed"))
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    module.run("config/etl_A.yaml")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["cohort_sql_performed"] is False
    assert data["cohort_overlap"] is None
    out = capsys.readouterr().out
    assert "AUDIT: COMPLETED" in out
    assert "exception_visits" not in out


def test_nonzero_exception_requires_restricted_password(
    tmp_path, monkeypatch,
):
    p = _private_path(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "verify_manifest", lambda: None)
    monkeypatch.setattr(module, "validate_private_metadata", lambda: 1)
    monkeypatch.delenv("OHDSI_QC_PASSWORD", raising=False)
    with pytest.raises(module.AuditBlocked, match="OHDSI_QC_PASSWORD"):
        module.run("config/etl_A.yaml")
    assert not p.exists()


def test_nonzero_path_uses_readonly_connection_and_aggregate_only(
    tmp_path, monkeypatch, capsys,
):
    p = _private_path(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "verify_manifest", lambda: None)
    monkeypatch.setattr(module, "validate_private_metadata", lambda: 2)
    monkeypatch.setattr(module, "verified_server_config", lambda *a: {})
    monkeypatch.setattr(module, "inspect_frozen", lambda *a: None)
    monkeypatch.setattr(module, "query_overlap",
                        lambda conn, expected: _valid_columns())
    monkeypatch.setattr(module, "make_restricted_odbc_string",
                        lambda cfg, db, pwd: f"synthetic-{db}")
    monkeypatch.setenv("OHDSI_QC_PASSWORD", "synthetic_only")

    class Conn:
        def close(self):
            pass

    class Pyo:
        def connect(self, connection_string, **kwargs):
            assert module.FROZEN_DATABASE in connection_string
            assert kwargs["autocommit"] is True
            return Conn()

    monkeypatch.setitem(sys.modules, "pyodbc", Pyo())
    study_path = (
        tmp_path / "study_definitions" / "stage_c_stroke_d0_v1.json"
    )
    study_path.parent.mkdir(parents=True)
    study_path.write_text(json.dumps({
        "study_definition": "stage-c-stroke-d0-v1",
        "frozen_etl_sha": "887e6f4d60a6b185e58b3c9fe8887472b49777e3",
    }), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module.run("config/etl_A.yaml")
    assert p.exists()
    output = capsys.readouterr().out
    assert "AUDIT: COMPLETED" in output
    assert "exception_visits" not in output
    assert "9815" not in output
    assert "6001" not in output
