"""Synthetic tests for private CDM_SOURCE metadata prefill. No live SQL."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prefill_frozen_cdm_source_manifest.py"
spec = importlib.util.spec_from_file_location("prefill_frozen_cdm_source", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def blank_manifest():
    return {
        "cdm_source": {
            "cdm_source_name": "",
            "cdm_source_abbreviation": "",
            "cdm_holder": "",
            "source_description": "",
            "source_documentation_reference": "",
            "cdm_etl_reference": "",
            "source_release_date": "",
            "cdm_release_date": "",
            "cdm_version": "v5.4",
            "cdm_version_concept_id": 756265,
            "vocabulary_version": ""
        },
        "evidence": {
            "cdm_holder": "",
            "source_release_date": "",
            "cdm_release_date": "",
            "cdm_version_concept_id": "",
            "vocabulary_version": ""
        }
    }


def test_known_fields_filled_but_unverified_dates_not_invented():
    prior = blank_manifest()
    populated = mod.fill_from_verified(prior, "SYNTHETIC_vocab")
    assert prior["cdm_source"]["cdm_source_name"] == ""
    assert populated["cdm_source"]["cdm_version"] == "v5.4"
    assert populated["cdm_source"]["vocabulary_version"] == "SYNTHETIC_vocab"
    assert mod.FROZEN_SHA in populated["cdm_source"]["cdm_etl_reference"]
    assert "pcornet.org" in populated["cdm_source"]["source_documentation_reference"]
    assert populated["evidence"]["vocabulary_version"]
    assert populated["evidence"]["cdm_version_concept_id"]
    for field in mod.MISSING_FIELDS:
        assert populated["cdm_source"][field] == ""
        assert populated["evidence"][field] == ""


def test_existing_user_holder_and_dates_are_preserved():
    prior = blank_manifest()
    prior["cdm_source"]["cdm_holder"] = "SYNTHETIC institutional data holder"
    prior["cdm_source"]["source_release_date"] = "2026-01-01"
    prior["cdm_source"]["cdm_release_date"] = "2026-02-01"
    populated = mod.fill_from_verified(prior, "SYNTHETIC_vocab")
    for field in mod.MISSING_FIELDS:
        assert populated["cdm_source"][field] == prior["cdm_source"][field]


@pytest.mark.parametrize("field", ["cdm_etl_reference", "cdm_version", "vocabulary_version"])
def test_conflict_with_verified_data_refuses_to_overwrite(field):
    prior = blank_manifest()
    prior["cdm_source"][field] = "SYNTHETIC_WRONG"
    with pytest.raises(mod.PreparationBlocked):
        mod.fill_from_verified(prior, "SYNTHETIC_vocab")


def test_private_file_atomic_replacement_without_leaking_values(tmp_path, monkeypatch):
    root = tmp_path / "results" / "postfreeze_standard_qc"
    root.mkdir(parents=True)
    root.chmod(0o700)
    file = root / "cdm_source_manifest.json"
    file.write_text(json.dumps(blank_manifest()), encoding="utf-8")
    file.chmod(0o600)
    monkeypatch.setattr(mod, "PRIVATE_DIR", root.resolve())
    monkeypatch.setattr(mod, "PRIVATE_FILE", file.resolve())
    populated = mod.fill_from_verified(blank_manifest(), "SYNTHETIC_vocab")
    mod.save_private_manifest(populated)
    assert json.loads(file.read_text()) == populated
    assert file.stat().st_mode & 0o777 == 0o600
    assert list(root.iterdir()) == [file]


def test_symlink_private_manifest_refused(tmp_path, monkeypatch):
    root = tmp_path / "results" / "postfreeze_standard_qc"
    root.mkdir(parents=True)
    root.chmod(0o700)
    file = root / "cdm_source_manifest.json"
    actual = root / "target.json"
    actual.write_text(json.dumps(blank_manifest()), encoding="utf-8")
    actual.chmod(0o600)
    file.symlink_to(actual)
    monkeypatch.setattr(mod, "PRIVATE_DIR", root.resolve())
    monkeypatch.setattr(mod, "PRIVATE_FILE", file)
    with pytest.raises(mod.PreparationBlocked):
        mod.read_manifest()


def test_preview_does_not_read_database_or_write_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prefill_frozen_cdm_source_manifest"])
    monkeypatch.setattr(mod, "read_manifest",
                        lambda: pytest.fail("preview cannot open manifest"))
    monkeypatch.setattr(mod, "verified_vocabulary",
                        lambda *_: pytest.fail("preview cannot open SQL"))
    mod.main()
    assert "PREVIEW ONLY" in capsys.readouterr().out


def test_merge_failed_before_private_file_write(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prefill_frozen_cdm_source_manifest", "--execute-local"])
    monkeypatch.setattr(mod, "read_manifest", blank_manifest)
    monkeypatch.setattr(mod, "verified_vocabulary", lambda _: "SYNTHETIC_vocab")
    monkeypatch.setattr(mod, "fill_from_verified", lambda *_:
                        (_ for _ in ()).throw(ValueError("private secret synthetic")))
    monkeypatch.setattr(mod, "save_private_manifest",
                        lambda _: pytest.fail("should not write on merge failure"))
    with pytest.raises(SystemExit):
        mod.main()
    visible = capsys.readouterr().err
    assert "BLOCKED [MERGE]" in visible
    assert "private secret" not in visible


def test_success_status_prints_no_metadata(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prefill_frozen_cdm_source_manifest", "--execute-local"])
    monkeypatch.setattr(mod, "read_manifest", blank_manifest)
    monkeypatch.setattr(mod, "verified_vocabulary", lambda _: "SYNTHETIC_SECRET_VOCAB")
    monkeypatch.setattr(mod, "save_private_manifest", lambda _: None)
    mod.main()
    visible = capsys.readouterr().out
    assert "PREFILL: PASS" in visible
    assert "SYNTHETIC_SECRET_VOCAB" not in visible
