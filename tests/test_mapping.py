from pcornet_omop_validation.mapping import DOMAIN_MAP


def test_core_domain_map_present():
    assert DOMAIN_MAP["PCORnet_DEMOGRAPHIC"] == ["person"]
    assert "condition_occurrence" in DOMAIN_MAP["PCORnet_DIAGNOSIS"]
    assert "measurement" in DOMAIN_MAP["PCORnet_LAB_RESULT_CM"]
