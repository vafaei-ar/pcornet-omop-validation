from __future__ import annotations

"""Read-only metadata audit for manuscript CDM/vocabulary/source-version wording.

This audit deliberately separates three different version concepts that should not be
collapsed in the manuscript:

1. the configured/pinned OMOP structural CDM release used to obtain the OHDSI DDL;
2. the loaded Athena vocabulary snapshot, recorded both through database vocabulary
   metadata and (when available) source-file fingerprints from the ETL audit trail;
3. the source PCORnet CDM version, if that version is actually recoverable from a
   HARVEST-style source table or other explicit version metadata.

The audit reads metadata only. It does not inspect or write patient-level rows and does
not modify the frozen target database.
"""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _schema(value: object) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe schema: {s!r}")
    return s


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _table_exists(con, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            text(
                "SELECT COUNT_BIG(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"
            ),
            {"s": schema, "t": table},
        ).scalar_one()
    )


def _columns(con, schema: str, table: str) -> list[str]:
    rows = con.execute(
        text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t ORDER BY ORDINAL_POSITION"
        ),
        {"s": schema, "t": table},
    ).fetchall()
    return [str(row[0]) for row in rows]


def _find_source_version_table(con, schema: str) -> tuple[str | None, list[str]]:
    rows = con.execute(
        text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA=:s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
        ),
        {"s": schema},
    ).fetchall()
    names = [str(r[0]) for r in rows]
    preferred = [
        name
        for name in names
        if name.upper() in {"PCORNET_HARVEST", "HARVEST"}
        or name.upper().endswith("_HARVEST")
    ]
    for name in preferred:
        cols = _columns(con, schema, name)
        version_cols = [c for c in cols if "VERSION" in c.upper()]
        if version_cols:
            return name, version_cols
    return (preferred[0], []) if preferred else (None, [])


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    cfg = load_etl_config(config_path)
    sql_cfg = cfg.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"))
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"))
    configured_cdm_version = str(cfg.raw.get("etl", {}).get("cdm_version") or "")
    common_data_model_cfg = (cfg.raw.get("downloads", {}) or {}).get("common_data_model", {}) or {}
    configured_release = str(common_data_model_cfg.get("release") or f"v{configured_cdm_version}")
    configured_source_repo = str(common_data_model_cfg.get("source") or "OHDSI/CommonDataModel")

    dependencies_path = cfg.audit_dir / "dependencies.json"
    vocabulary_load_path = cfg.audit_dir / "vocabulary_load.json"
    dependencies = _read_json(dependencies_path)
    vocabulary_load = _read_json(vocabulary_load_path)

    dependency_cdm: dict[str, Any] | None = None
    if dependencies:
        for asset in dependencies.get("assets", []):
            if str(asset.get("name", "")).lower() == "ohdsi commondatamodel":
                dependency_cdm = {
                    "name": asset.get("name"),
                    "version": asset.get("version"),
                    "url": asset.get("url"),
                    "sha256": asset.get("sha256"),
                }
                break

    vocab_file_fingerprints: list[dict[str, Any]] = []
    if vocabulary_load:
        for item in vocabulary_load.get("tables", []):
            vocab_file_fingerprints.append(
                {
                    "file_name": Path(str(item.get("file") or "")).name,
                    "table": item.get("table"),
                    "source_rows": item.get("source_rows"),
                    "target_rows": item.get("target_rows"),
                    "sha256": item.get("sha256"),
                    "status": item.get("status"),
                }
            )

    required_vocabularies = [
        str(v) for v in (cfg.raw.get("vocabulary", {}) or {}).get("require_vocabularies", [])
    ]

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            target_database = str(con.execute(text("SELECT DB_NAME()")) .scalar_one())

            cdm_source: dict[str, Any] = {
                "table_present": _table_exists(con, target_schema, "cdm_source"),
                "row_count": None,
                "rows": [],
            }
            if cdm_source["table_present"]:
                cdm_source["row_count"] = int(
                    con.execute(text(f"SELECT COUNT_BIG(*) FROM [{target_schema}].[cdm_source]")).scalar_one()
                )
                cols = {c.lower(): c for c in _columns(con, target_schema, "cdm_source")}
                safe_names = [
                    "cdm_source_name",
                    "cdm_source_abbreviation",
                    "cdm_holder",
                    "source_release_date",
                    "cdm_release_date",
                    "cdm_version",
                    "vocabulary_version",
                    "cdm_version_concept_id",
                ]
                selected = [cols[name] for name in safe_names if name in cols]
                if selected and cdm_source["row_count"]:
                    sql = "SELECT " + ",".join(f"[{c}]" for c in selected) + f" FROM [{target_schema}].[cdm_source]"
                    rows = con.execute(text(sql)).mappings().all()
                    cdm_source["rows"] = [
                        {str(k): _jsonable(v) for k, v in dict(row).items()} for row in rows
                    ]

            vocabulary_metadata: list[dict[str, Any]] = []
            vocabulary_table_present = _table_exists(con, target_schema, "vocabulary")
            if vocabulary_table_present:
                params: dict[str, Any] = {}
                if required_vocabularies:
                    placeholders = []
                    for i, vocab_id in enumerate(required_vocabularies):
                        key = f"v{i}"
                        params[key] = vocab_id
                        placeholders.append(f":{key}")
                    where = "WHERE vocabulary_id IN (" + ",".join(placeholders) + ")"
                else:
                    where = ""
                rows = con.execute(
                    text(
                        f"SELECT vocabulary_id,vocabulary_name,vocabulary_reference,"
                        f"vocabulary_version,vocabulary_concept_id "
                        f"FROM [{target_schema}].[vocabulary] {where} ORDER BY vocabulary_id"
                    ),
                    params,
                ).mappings().all()
                vocabulary_metadata = [dict(row) for row in rows]

            source_version_table, source_version_columns = _find_source_version_table(con, source_schema)
            source_version: dict[str, Any] = {
                "table": source_version_table,
                "version_columns": source_version_columns,
                "distinct_version_values": {},
            }
            if source_version_table and source_version_columns:
                for column in source_version_columns:
                    values = con.execute(
                        text(
                            f"SELECT DISTINCT CONVERT(nvarchar(255),[{column}]) AS value "
                            f"FROM [{source_schema}].[{source_version_table}] "
                            f"WHERE [{column}] IS NOT NULL ORDER BY value"
                        )
                    ).scalars().all()
                    source_version["distinct_version_values"][column] = [str(v) for v in values]

            source_harvest_columns = (
                _columns(con, source_schema, source_version_table)
                if source_version_table
                else []
            )
    finally:
        engine.dispose()

    structural_release_evidence = {
        "configured_cdm_version": configured_cdm_version,
        "configured_common_data_model_release": configured_release,
        "configured_common_data_model_repository": configured_source_repo,
        "dependency_manifest": dependency_cdm,
        "schema_code_evidence": {
            "frozen_etl_sha": FROZEN_ETL_SHA,
            "expected_ddl_filename": "OMOPCDM_sql_server_5.4_ddl.sql",
            "note": (
                "The frozen schema loader obtains the OHDSI CommonDataModel release pinned in config "
                "and applies the SQL Server OMOP 5.4 DDL. This is structural-release provenance and is "
                "distinct from the Athena vocabulary snapshot."
            ),
        },
    }

    source_version_status = "explicit_version_metadata_found"
    if not source_version_table:
        source_version_status = "no_harvest_style_table_found"
    elif not source_version_columns:
        source_version_status = "harvest_style_table_found_but_no_version_column"
    elif not any(source_version["distinct_version_values"].values()):
        source_version_status = "version_columns_found_but_no_nonnull_values"

    payload: dict[str, Any] = {
        "status": "manuscript_metadata_version_audit_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "target_database": target_database,
        "target_schema": target_schema,
        "source_schema": source_schema,
        "structural_cdm_release": structural_release_evidence,
        "target_cdm_source_metadata": cdm_source,
        "athena_vocabulary_snapshot": {
            "required_vocabulary_ids": required_vocabularies,
            "database_vocabulary_metadata": vocabulary_metadata,
            "vocabulary_load_audit_path": str(vocabulary_load_path),
            "source_file_fingerprints": vocab_file_fingerprints,
        },
        "source_pcornet_version": {
            "status": source_version_status,
            **source_version,
            "harvest_columns": source_harvest_columns,
            "interpretation": (
                "Only explicit source version metadata is treated as evidence of the PCORnet CDM version. "
                "Table/column shape alone is not used to infer an exact PCORnet release."
            ),
        },
        "manuscript_guardrail": (
            "Report OMOP structural CDM release and Athena vocabulary snapshot as separate version dimensions. "
            "Do not state an exact PCORnet source version unless explicit source metadata supports it."
        ),
        "disclosure_review": {
            "read_only": True,
            "patient_level_queries": False,
            "patient_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    out_dir = (
        Path(output_dir)
        if output_dir
        else cfg.audit_dir.parent / "publication_analysis" / "manuscript_metadata"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "manuscript_metadata_version_audit.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("status:", payload["status"])
    print("target database:", target_database)
    print("configured OMOP CDM version:", configured_cdm_version)
    print("configured OHDSI CommonDataModel release:", configured_release)
    print("cdm_source rows:", cdm_source.get("row_count"))
    print("source PCORnet version status:", source_version_status)
    print("source PCORnet version values:", json.dumps(source_version["distinct_version_values"], sort_keys=True))
    print("vocabulary metadata rows:", len(vocabulary_metadata))
    print("output:", output_path)
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only manuscript CDM/vocabulary/source-version metadata audit")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    args = p.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
