import importlib.util
import pathlib

p=pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'audit_stage_d_90d_observability_mortality.py'
spec=importlib.util.spec_from_file_location('audit90',p)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_sql_semantics_safety():
    m.self_test()
    assert 'FROM #audit90' in m._select_aggregates('1=1','source_d0_all')
    assert 'GROUP BY admission_era' in m._select_aggregates('1=1','era','admission_era')


def test_anchor_integrity():
    orig={'results': {
        'D0_reproduction': {'source_patients':9815,'lineage_faithful_omop_patients':6001,'shared_exact_index_patients':6001},
        'primary_fixed_index_90d': {'label_counts': {'eligible':3822}},
        'secondary_end_to_end_90d': {'source_eligible':6508,'omop_eligible':3822,'source_events':1798,'omop_events':1132},
    }}
    rows=[
        {'group_type':'source_d0_all','group_label':'source_d0_all','n':9815,'source_covered90':6508,'source_events90_covered':1798},
        {'group_type':'omop_lineage_retained','group_label':'omop_lineage_retained','n':6001,'target_covered90':3822,'target_events90_covered':1132},
        {'group_type':'shared_exact_index','group_label':'shared_exact_index','n':6001},
        {'group_type':'shared_coverage_pair','group_label':'source=1,target=1','n':3822},
    ]
    m._validate_matches(rows,orig)
    rows[0]['source_events90_covered']=1799
    try:
        m._validate_matches(rows,orig)
        raise AssertionError('numerator mismatch should fail')
    except RuntimeError as exc:
        assert 'source 90d numerator' in str(exc)

if __name__ == '__main__':
    test_sql_semantics_safety()
    test_anchor_integrity()
    print('2 offline unit tests passed')