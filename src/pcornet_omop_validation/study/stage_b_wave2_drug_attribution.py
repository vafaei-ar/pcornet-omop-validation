from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
ROUTE_TABLE = "etl_drug_event_route"
XWALK_TABLE = "etl_drug_exposure_xwalk"


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return "unknown"


def _schema(value: object) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server schema: {s!r}")
    return s


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    target_schema = _schema(config.raw["sqlserver"].get("target_schema", "dbo"))
    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "drug"
    )
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for table in (ROUTE_TABLE, XWALK_TABLE, "drug_exposure"):
                if not table_exists(con, target_schema, table):
                    raise RuntimeError(f"Missing [{target_schema}].[{table}]")

            print("progress: materializing nonzero Drug semantic concept space", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#drug_concepts') IS NOT NULL DROP TABLE #drug_concepts")
            con.exec_driver_sql(f"""
                SELECT DISTINCT CAST(target_concept_id AS bigint) AS target_concept_id
                INTO #drug_concepts
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE target_concept_id<>0;
                CREATE UNIQUE CLUSTERED INDEX IX_drug_concepts ON #drug_concepts(target_concept_id);
            """)

            row = con.execute(text(f"""
                WITH target_space AS (
                  SELECT d.drug_exposure_id,d.person_id,d.drug_type_concept_id,d.route_concept_id,
                         CASE WHEN EXISTS (
                           SELECT 1 FROM [{target_schema}].[{XWALK_TABLE}] x
                           WHERE x.drug_exposure_id=d.drug_exposure_id
                         ) THEN 1 ELSE 0 END AS is_base_drug_derived
                  FROM [{target_schema}].[drug_exposure] d
                  JOIN #drug_concepts c ON c.target_concept_id=d.drug_concept_id
                )
                SELECT COUNT_BIG(*) AS target_rows,
                       SUM(CASE WHEN is_base_drug_derived=1 THEN 1 ELSE 0 END) AS base_drug_derived_rows,
                       SUM(CASE WHEN is_base_drug_derived=0 THEN 1 ELSE 0 END) AS other_provenance_rows,
                       COUNT_BIG(DISTINCT person_id) AS target_patients,
                       COUNT_BIG(DISTINCT CASE WHEN is_base_drug_derived=1 THEN person_id END) AS base_drug_derived_patients,
                       COUNT_BIG(DISTINCT CASE WHEN is_base_drug_derived=0 THEN person_id END) AS other_provenance_patients,
                       SUM(CASE WHEN is_base_drug_derived=0 AND drug_type_concept_id=0 THEN 1 ELSE 0 END) AS other_provenance_type_zero_rows,
                       SUM(CASE WHEN is_base_drug_derived=0 AND route_concept_id=0 THEN 1 ELSE 0 END) AS other_provenance_route_zero_rows
                FROM target_space
            """)).mappings().one()
            totals = {k:int(v or 0) for k,v in dict(row).items()}

            type_rows = [dict(r) for r in con.execute(text(f"""
                WITH target_space AS (
                  SELECT d.drug_exposure_id,d.person_id,d.drug_type_concept_id,
                         CASE WHEN EXISTS (
                           SELECT 1 FROM [{target_schema}].[{XWALK_TABLE}] x
                           WHERE x.drug_exposure_id=d.drug_exposure_id
                         ) THEN 'base_drug_lineage' ELSE 'other_audited_provenance' END AS provenance
                  FROM [{target_schema}].[drug_exposure] d
                  JOIN #drug_concepts c ON c.target_concept_id=d.drug_concept_id
                )
                SELECT provenance,drug_type_concept_id,COUNT_BIG(*) AS rows,
                       COUNT_BIG(DISTINCT person_id) AS patients
                FROM target_space
                GROUP BY provenance,drug_type_concept_id
                ORDER BY provenance,drug_type_concept_id
            """)).mappings().all()]
    finally:
        engine.dispose()

    summary = {
        "status":"stage_b_wave2_drug_secondary_attribution_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "method":"Secondary attribution only. Primary Drug concordance is defined independently from this xwalk classification.",
        "totals":totals,
        "type_concept_by_provenance":type_rows,
    }
    (out/"stage_b_wave2_drug_attribution.json").write_text(json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8")
    if type_rows:
        with (out/"drug_target_provenance_type_concept.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(type_rows[0].keys()))
            w.writeheader(); w.writerows(type_rows)

    print("status: stage_b_wave2_drug_secondary_attribution_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"target_rows_in_drug_concept_space: {totals['target_rows']}")
    print(f"base_drug_derived_rows: {totals['base_drug_derived_rows']}")
    print(f"other_provenance_rows: {totals['other_provenance_rows']}")
    print(f"other_provenance_type_zero_rows: {totals['other_provenance_type_zero_rows']}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser=argparse.ArgumentParser(description="Stage B Wave 2 Drug secondary provenance attribution")
    parser.add_argument("--config",required=True)
    parser.add_argument("--output-dir")
    args=parser.parse_args()
    run(args.config,args.output_dir)


if __name__=="__main__":
    main()
