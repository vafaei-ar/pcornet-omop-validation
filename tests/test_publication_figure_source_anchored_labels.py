"""Guard against misleading independence labels in generated publication figures."""
from pathlib import Path

def test_publication_figures_label_source_anchored_primary_cohort():
    source = Path("src/pcornet_omop_validation/study/publication_figures.py").read_text(encoding="utf-8")
    assert "Source-anchored D0 cohort" in source
    assert "SOURCE-ANCHORED end-to-end" in source
    assert "source-anchored cohort retention" in source
    assert "Independent base cohort" not in source
    assert "INDEPENDENT end-to-end" not in source
    assert "independent cohort construction" not in source
