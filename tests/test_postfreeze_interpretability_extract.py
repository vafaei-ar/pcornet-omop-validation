from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("pandas")

from pcornet_omop_validation.study import postfreeze_interpretability_extract as extractor


def test_row_level_export_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="--confirm-private"):
        extractor.run("does-not-exist.yaml", "results/private_inputs", False)


def test_export_refuses_path_outside_ignored_results(tmp_path):
    with pytest.raises(ValueError, match="under ignored results"):
        extractor.run("does-not-exist.yaml", str(tmp_path / "private"), True)


def test_frozen_sha_unchanged():
    assert extractor.FROZEN_SHA == "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
