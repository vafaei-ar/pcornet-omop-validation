from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA_V1 = ROOT / "study_definitions" / "artifacts" / "publication_figure_data_v1.json"
DATA = ROOT / "study_definitions" / "artifacts" / "publication_figure_data_v2.json"
EXPECTED_V1_SHA256 = "6039aa9ee4dafa3d2292e341555a75a7710bcb234849c2832bbc3413733ee523"


def test_original_frozen_publication_aggregate_remains_immutable() -> None:
    payload = DATA_V1.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_V1_SHA256
    data = json.loads(payload)
    assert data["version"] == "publication_figure_data_v1"
    assert data["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def test_current_publication_aggregate_version_and_provenance() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert data["version"] == "publication_figure_data_v2"
    assert data["frozen_etl_sha"] == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
    assert data["provenance"]["aggregate_only"] is True
    assert data["provenance"]["base_artifact"].endswith("publication_figure_data_v1.json")

    for phenotype in ("D0", "D1", "D3"):
        fallback = data["stage_c"]["encounter_date_fallback"][phenotype]
        assert fallback["pcornet"] == fallback["target"] == fallback["shared"]
        assert fallback["jaccard"] == 1.0
        assert fallback["exact_index_percent"] == 100.0

    for window in ("30_day", "90_day"):
        fallback = data["stage_d"]["fallback_end_to_end"][window]
        assert fallback["risk_difference_pp"] == 0.0
        assert fallback["rr"] == 1.0
        assert fallback["pcornet_eligible"] == fallback["target_eligible"] == fallback["label_agreement"]

    complement = data["stage_d"]["source_only_complement"]["90_day"]
    assert complement["source_only_eligible"] == 2686
    assert complement["source_only_events"] == 666
    assert complement["source_only_risk_percent"] < complement["shared_risk_percent"]


def test_only_canonical_publication_modules_remain() -> None:
    study = ROOT / "src" / "pcornet_omop_validation" / "study"
    assert (study / "publication_figures.py").is_file()
    assert (study / "publication_tables.py").is_file()
    legacy = [
        "publication_figure_panels_main.py",
        "publication_figure_panels_extended.py",
        "publication_figure_style.py",
        "publication_jamia_assets.py",
        "publication_jamia_final.py",
        "publication_jamia_main_v2.py",
        "publication_jamia_main_v3.py",
        "publication_jamia_tables.py",
    ]
    assert not [name for name in legacy if (study / name).exists()]


def test_publication_figure_registry_and_locked_invariants() -> None:
    pytest.importorskip("matplotlib")
    from pcornet_omop_validation.study import publication_figures as figures

    data = json.loads(DATA.read_text(encoding="utf-8"))
    figures.validate_data(data)
    assert figures.DEFAULT_DATA == Path("study_definitions/artifacts/publication_figure_data_v2.json")
    assert figures.FIGURE_NAMES == (
        "Figure1_reproducibility_breakpoint",
        "Figure2_phenotype_mechanism",
        "Figure3_outcome_estimands",
        "Figure4_model_reproducibility",
        "ExtendedDataFigure1_semantic_fidelity",
        "ExtendedDataFigure2_additional_reproducibility",
        "ExtendedDataFigure3_calibration",
    )
    assert set(figures.BUILDERS) == set(figures.FIGURE_NAMES)


def test_publication_table_registry_accepts_current_aggregate() -> None:
    from pcornet_omop_validation.study import publication_tables as tables

    data = json.loads(DATA.read_text(encoding="utf-8"))
    specs = {**tables.main_tables(data), **tables.supplementary_tables(data)}
    assert list(tables.main_tables(data)) == ["Table1", "Table2", "Table3"]
    assert list(tables.supplementary_tables(data)) == [f"S{i}" for i in range(1, 15)]
    assert len(specs) == 17
