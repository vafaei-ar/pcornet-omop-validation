from pathlib import Path

import pytest

from pcornet_omop_validation.etl.config import load_etl_config


def test_etl_config_requires_sqlserver_backend(tmp_path: Path):
    config_path = tmp_path / "etl.yaml"
    config_path.write_text(
        """
etl:
  cdm_version: '5.4'
  backend: postgresql
source:
  parquet_dir: /tmp/source
sqlserver: {}
vocabulary:
  directory: /tmp/vocab
output:
  parquet_dir: /tmp/output
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sqlserver"):
        load_etl_config(config_path)


def test_etl_config_pins_cdm_54(tmp_path: Path):
    config_path = tmp_path / "etl.yaml"
    config_path.write_text(
        """
etl:
  cdm_version: '6.0'
  backend: sqlserver
source:
  parquet_dir: /tmp/source
sqlserver: {}
vocabulary:
  directory: /tmp/vocab
output:
  parquet_dir: /tmp/output
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="5.4"):
        load_etl_config(config_path)
