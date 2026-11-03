"""Offline tests for Qiran's post-freeze audit. No database required."""
import pytest
from scripts import qiran_postfreeze_audit as qa

def test_anchor_guard():
    counts = dict(source_d0=9815, omop_d0=6001, shared_d0=6001, source_only_d0=3814, omop_only_d0=0, exact_index_date_d0=6001)
    qa.validate_anchors(counts, {"omop_person_rows":27089})
    counts["source_only_d0"] = 3813
    with pytest.raises(RuntimeError, match="NOT reproduced"):
        qa.validate_anchors(counts, {"omop_person_rows":27089})

def test_schema_validation():
    assert qa.schema_name("dbo") == "dbo"
    with pytest.raises(ValueError):
        qa.schema_name("dbo]; DROP TABLE person;--")

def test_d0_rules():
    sql = qa.sql_setup("src","omop")
    assert "'EI','IP'" in sql
    assert "COALESCE(x.dx_date" in sql
    assert "etl_condition_occurrence_xwalk" in sql
    assert "#src_d0" in sql and "#omop_d0" in sql
    # Inherited Stage C/D: first choose source D0 index stroke, then check
    # whether that exact diagnosis and encounter were materialized in OMOP.
    assert "FROM #src_d0 d" in sql
    assert "FROM #d0_candidates d" not in sql
    assert "co.condition_occurrence_id) rn" in sql
    assert "SELECT * INTO #omop_d0 FROM chosen WHERE rn=1;" in sql
    assert "FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18" not in sql.split(
        "SELECT * INTO #omop_d0 FROM chosen")[1]

def test_candidate_missingness_denominator():
    sql = qa.sql_candidate_missingness("src")
    assert "PCORnet_DIAGNOSIS" in sql
    assert "COUNT_BIG(*) n" in sql
    assert "SUM(CASE WHEN d.DX_DATE IS NULL" in sql
    assert "#qiran_src" not in sql

def test_overlap_uses_exact_event_keys():
    sql = qa.sql_cross_stream_overlap("src","omop")
    assert "dg.source_code=cg.source_code" in sql
    assert "dg.event_date=cg.event_date" in sql
    assert "dg.encounterid=cg.encounterid" in sql

def test_small_count_suppression():
    sample = [{"cohort":"shared","year_band":"2020-24","n":9,"missing_dx_n":1},
              {"cohort":"source_only","year_band":"2020-24","n":33,"missing_dx_n":29}]
    screened = qa.suppress_partition(sample,"year_band")
    assert all(r["n"] is None for r in screened)
    assert all(r["suppressed"] for r in screened)

def test_snapshot_reference_table_fingerprint():
    counts = {
        "source_diagnosis_rows": 11484577,
        "omop_person_rows": 27089,
        "omop_visit_rows": 1510957,
        "omop_condition_rows": 7315572,
    }
    assert qa.snapshot_mismatches(counts) == {}
    changed = counts | {"omop_person_rows": 75274, "source_diagnosis_rows": 10000}
    mismatch = qa.snapshot_mismatches(changed)
    assert mismatch["omop_person_rows"] == (27089, 75274)
    assert mismatch["source_diagnosis_rows"] == (11484577, 10000)

def test_snapshot_diagnostic_queries_are_aggregate_only():
    import inspect
    source = inspect.getsource(qa.snapshot_counts).lower()
    assert "count_big(*)" in source
    assert "insert into" not in source
    assert "update " not in source
    assert "delete from" not in source

def test_index_date_concordance_is_hard_anchor():
    counts = dict(source_d0=9815, omop_d0=6001, shared_d0=6001,
                  source_only_d0=3814, omop_only_d0=0, exact_index_date_d0=6000)
    with pytest.raises(RuntimeError, match="NOT reproduced"):
        qa.validate_anchors(counts, {"omop_person_rows":27089})
    assert "exact_index_date_d0" in qa.sql_anchors()
