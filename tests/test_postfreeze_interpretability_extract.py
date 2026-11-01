from __future__ import annotations

import pytest
import stat
from pathlib import Path

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



def test_private_export_path_is_inside_repo_results_only(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "results").mkdir(parents=True)
    monkeypatch.setattr(extractor, "PRIVATE_ROOT", root / "results")
    good = root / "results" / "private_inputs"
    assert extractor._private_output_path(str(good)) == good
    with pytest.raises(ValueError, match="under ignored results"):
        extractor._private_output_path(str(tmp_path / "results" / "private_inputs"))
    with pytest.raises(ValueError, match="under ignored results"):
        extractor._private_output_path(str(root / "results"))
    target = tmp_path / "outside"
    target.mkdir()
    (root / "results" / "alias").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        extractor._private_output_path(str(root / "results" / "alias" / "cohorts"))


def test_private_export_directory_fails_closed(tmp_path, monkeypatch):
    out = tmp_path / "private_inputs"
    out.mkdir(mode=0o700)
    out.chmod(0o700)
    work = extractor._prepare_private_output(out)
    assert work == out / "private_rerun"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError, match="not empty"):
        extractor._prepare_private_output(out)


def test_private_export_rejects_group_or_world_readable_directory(tmp_path):
    out = tmp_path / "private_inputs"
    out.mkdir()
    out.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        extractor._prepare_private_output(out)


def test_private_parquet_file_uses_0600_and_refuses_overwrite(
    tmp_path, monkeypatch,
):
    path = tmp_path / "synthetic.parquet"
    frame = pd.DataFrame({"feature": [1, 2]})
    def fake_to_parquet(self, handle, index):
        assert index is False
        handle.write(b"synthetic-only-not-a-parquet")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", fake_to_parquet)
    extractor._write_private_parquet(frame, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"synthetic-only-not-a-parquet"
    with pytest.raises(FileExistsError):
        extractor._write_private_parquet(frame, path)


def test_private_population_rejects_null_and_duplicate_identifiers(monkeypatch):
    monkeypatch.setitem(extractor.EXPECTED, "pcornet_full", (3, 1))
    cols = {
        "patid": ["p1", "p2", "p3"],
        "index_date": ["2024-01-01"] * 3,
        "outcome": [0, 1, 0],
        **{name: [1.0, 2.0, 3.0] for name in extractor.FEATURES},
    }
    clean = pd.DataFrame(cols)
    parsed = extractor._prepare_population(clean, "pcornet_full")
    assert list(parsed.index) == cols["patid"]
    duplicated = clean.copy()
    duplicated.loc[1, "patid"] = "p1"
    with pytest.raises(RuntimeError, match="duplicated"):
        extractor._prepare_population(duplicated, "pcornet_full")
    missing = clean.copy()
    missing.loc[1, "patid"] = None
    with pytest.raises(RuntimeError, match="null patient"):
        extractor._prepare_population(missing, "pcornet_full")
    null_date = clean.copy()
    null_date.loc[1, "index_date"] = None
    with pytest.raises(RuntimeError, match="index date"):
        extractor._prepare_population(null_date, "pcornet_full")
