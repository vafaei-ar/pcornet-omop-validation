"""Offline tests. No real SQL Server/database connection is attempted."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sensitivity_stage_d_deathaware_90d as m


def fake_summary():
    return dict(n=6,original_eligible=3,original_events=2,eligible=4,events=3,
                added=2,removed=1,early_deaths=2,eligible_early_deaths=2,
                excluded_early_deaths=0,added_early_deaths=2,
                invalid_pre_discharge_deaths=1,first_events_on_death_day=1,
                first_events_after_death=0,changed_positive_to_negative=1,
                eligible_mean_age=70.1)


def test_deathaware_policy_cutoffs_and_ties():
    discharge=date(2020,1,1)
    assert m.deathaware_cutoff(discharge,None)==date(2020,3,31)
    assert m.deathaware_cutoff(discharge,date(2020,1,15))==date(2020,1,15)
    assert m.deathaware_cutoff(discharge,date(2020,3,31))==date(2020,3,31)
    assert m.deathaware_cutoff(discharge,date(2020,4,1))==date(2020,3,31)
    assert m.deathaware_cutoff(discharge,date(2019,12,31)) is None
    cutoff=m.deathaware_cutoff(discharge,date(2020,1,15))
    assert m.valid_event(date(2020,1,2),discharge,cutoff)
    assert m.valid_event(date(2020,1,15),discharge,cutoff)  # death-day ambiguity retained
    assert not m.valid_event(date(2020,1,16),discharge,cutoff)
    assert not m.valid_event(discharge,discharge,cutoff)
    assert not m.valid_event(None,discharge,cutoff)
    assert not m.valid_event(date(2020,1,2),discharge,None)


def test_sql_reuses_frozen_semantics_and_avoids_permanent_writes():
    m.self_test()
    q=m.deathaware_sql('dbo','omop')
    assert 'FROM #audit90 a' in q
    assert 'JOIN #src_d0 s' in q
    assert 'LEFT JOIN #omop_d0 o' in q
    assert 'CAST(en.ENR_END_DATE AS date)>=cut.src_end' in q
    assert 'op.observation_period_end_date>=cut.tgt_end' in q
    assert 'sfirst.first_date<=cut.src_end' in q
    assert 'tfirst.first_date<=cut.tgt_end' in q
    assert 'a.source_death_date < a.discharge_date THEN 0' in q
    assert 'a.target_death_date < o.omop_discharge_date THEN 0' in q
    for disallowed in ('DROP TABLE [','ALTER TABLE [','UPDATE [','DELETE FROM [','INSERT INTO ['):
        assert disallowed not in q.upper()
    assert 'WHERE same_index=1 AND src_eligible=1 AND tgt_eligible=1' in m.fixed_sql()


def test_aggregate_rollforward_checks():
    v=fake_summary()
    m.validate_aggregate(v)
    bad={**v,'eligible':5}
    with pytest.raises(RuntimeError,match='roll-forward'):
        m.validate_aggregate(bad)
    bad={**v,'added_early_deaths':1}
    with pytest.raises(RuntimeError,match='without a recorded early death'):
        m.validate_aggregate(bad)
    bad={**v,'removed':2,'eligible':3}
    with pytest.raises(RuntimeError,match='reasons beyond invalid'):
        m.validate_aggregate(bad)


def test_risk_comparison_and_zero_denominator():
    r=m.compare({'events':14,'eligible':50},{'events':12,'eligible':40})
    assert r['source_risk']==pytest.approx(0.28)
    assert r['omop_risk']==pytest.approx(0.30)
    assert r['absolute_difference_percentage_points']==pytest.approx(2.0)
    assert r['omop_over_source_risk_ratio']==pytest.approx(0.30/0.28)
    z=m.compare({'events':0,'eligible':0},{'events':0,'eligible':0})
    assert z['absolute_difference_percentage_points'] is None
    assert z['omop_over_source_risk_ratio'] is None


def test_locked_diagnostic_provenance(tmp_path,monkeypatch):
    p=tmp_path/'stage_d.json'
    p.write_text('{}')
    d=tmp_path/'diagnostic.json'
    correct={
        'status':'postfreeze_observability_mortality_90d_diagnostic_complete',
        'cohort_and_outcome_anchors_match':True,
        'frozen_etl_sha':m.FROZEN_ETL_SHA,
        'parent_stage_d_sha256':'checked_hash',
        'study_definition_sha256':'checked_hash',
        'd0_definition_sha256':'checked_hash',
    }
    monkeypatch.setattr(m,'_sha256',lambda _: 'checked_hash')
    d.write_text(json.dumps(correct))
    assert m.load_diagnostic(d,p)==correct
    wrong={**correct,'parent_stage_d_sha256':'wrong'}
    d.write_text(json.dumps(wrong))
    with pytest.raises(RuntimeError,match='anchor mismatch'):
        m.load_diagnostic(d,p)


def test_fixed_original_anchor_must_include_labels():
    original={'results':{'primary_fixed_index_90d':{'label_counts':{
      'eligible':4,'source_events':2,'omop_events':2,
      'both_positive':2,'source_only_positive':0,'omop_only_positive':0,'both_negative':2,
    }}}}
    actual=dict(original['results']['primary_fixed_index_90d']['label_counts'])
    m.validate_original_fixed(actual,original)
    actual['both_positive']=1
    with pytest.raises(RuntimeError,match='fixed-index 90-day both_positive mismatch'):
        m.validate_original_fixed(actual,original)