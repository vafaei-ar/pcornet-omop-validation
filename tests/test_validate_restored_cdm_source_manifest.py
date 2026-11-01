"""Synthetic offline-only validation of restored CDM_SOURCE provenance."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import stat

import pytest

FILE = (Path(__file__).resolve().parents[1] / "scripts"
        / "validate_restored_cdm_source_manifest.py")
spec = importlib.util.spec_from_file_location("cdm_source_manifest", FILE)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def synthetic_record():
    return {
        "cdm_source": {
            "cdm_source_name": "Synthetic isolated restored test database",
            "cdm_source_abbreviation": "SYNTH",
            "cdm_holder": "Synthetic holder",
            "source_description": (
                "Synthetic PCORnet test data, isolated restored-copy rehearsal; "
                "not a factual institutional source."
            ),
            "source_documentation_reference": "synthetic source specification",
            "cdm_etl_reference": "synthetic frozen code "
                                 + mod.FROZEN_SHA,
            "source_release_date": "2026-06-01",
            "cdm_release_date": "2026-06-15",
            "cdm_version": "v5.4",
            "cdm_version_concept_id": 756265,
            "vocabulary_version": "synthetic_v1",
        },
        "evidence": {
            "cdm_holder": "synthetic institutional authorization",
            "source_release_date": "synthetic source extraction log",
            "cdm_release_date": "synthetic ETL completion manifest",
            "cdm_version_concept_id": "synthetic v5.4 concept lookup",
            "vocabulary_version": "synthetic vocabulary metadata",
        },
    }


def test_complete_synthetic_manifest_passes_without_sql():
    mod.validate(synthetic_record())


@pytest.mark.parametrize("field,value", [
    ("cdm_source_name", ""),
    ("cdm_source_abbreviation", "X" * 26),
    ("cdm_holder", None),
    ("source_release_date", "2026-15-03"),
    ("cdm_release_date", "2026-1-1"),
    ("vocabulary_version", ""),
    ("cdm_version", "5.4.2"),
    ("cdm_version_concept_id", 0),
    ("cdm_version_concept_id", "756265"),
    ("cdm_etl_reference", "unfrozen commit"),
])
def test_missing_invented_or_incompatible_fields_block(field, value):
    obj = synthetic_record()
    obj["cdm_source"][field] = value
    with pytest.raises(mod.ManifestBlocked):
        mod.validate(obj)


def test_all_11_columns_must_match_official_v54():
    obj = synthetic_record()
    obj["cdm_source"].pop("cdm_source_name")
    with pytest.raises(mod.ManifestBlocked, match="11"):
        mod.validate(obj)
    obj = synthetic_record()
    obj["cdm_source"]["unexpected_column"] = "x"
    with pytest.raises(mod.ManifestBlocked, match="11"):
        mod.validate(obj)


def test_provenance_evidence_must_not_be_empty():
    for field in mod.REQUIRED_EVIDENCE:
        obj = synthetic_record()
        obj["evidence"][field] = ""
        with pytest.raises(mod.ManifestBlocked):
            mod.validate(obj)


def test_owner_only_path_and_no_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "results" / "postfreeze_standard_qc"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    monkeypatch.setattr(mod, "BASE", root.resolve())
    p = root / "local_cdm_source.json"
    p.write_text(json.dumps(synthetic_record()), encoding="utf-8")
    p.chmod(0o600)
    mod.validate_private_path(p)
    p.chmod(0o644)
    with pytest.raises(mod.ManifestBlocked):
        mod.validate_private_path(p)
    p.chmod(0o600)
    q = root / "link.json"
    q.symlink_to(p)
    with pytest.raises(mod.ManifestBlocked):
        mod.validate_private_path(q)


def test_cli_never_prints_protected_metadata(tmp_path, monkeypatch, capsys):
    root = tmp_path / "results" / "postfreeze_standard_qc"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    monkeypatch.setattr(mod, "BASE", root.resolve())
    p = root / "private.json"
    obj = synthetic_record()
    obj["cdm_source"]["cdm_holder"] = "SYNTHETIC_PRIVATE_HOLDER"
    p.write_text(json.dumps(obj), encoding="utf-8")
    p.chmod(0o600)
    monkeypatch.setattr(mod.argparse.ArgumentParser, "parse_args",
                        lambda *_: mod.argparse.Namespace(manifest=str(p)))
    mod.main()
    visible = capsys.readouterr()
    assert "PRELIMINARY PASS" in visible.out
    assert "SYNTHETIC_PRIVATE_HOLDER" not in visible.out + visible.err
    obj["cdm_source"]["source_release_date"] = "not-a-date"
    p.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.main()
    visible = capsys.readouterr()
    assert "BLOCKED" in visible.err
    assert "SYNTHETIC_PRIVATE_HOLDER" not in visible.out + visible.err
