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
ROUTE_TABLE = "etl_procedure_event_route"

DOMAINS = {
    "Condition": ("condition_occurrence", "condition_occurrence_id", "condition_concept_id", "etl_procedure_condition_xwalk", "condition_occurrence_id"),
    "Device": ("device_exposure", "device_exposure_id", "device_concept_id", "etl_device_exposure_xwalk", "device_exposure_id"),
    "Drug": ("drug_exposure", "drug_exposure_id", "drug_concept_id", "etl_drug_exposure_xwalk", "drug_exposure_id"),
    "Measurement": ("measurement", "measurement_id", "measurement_concept_id", "etl_measurement_xwalk", "measurement_id"),
    "Observation": ("observation", "observation_id", "observation_concept_id", "etl_observation_xwalk", "observation_id"),
    "Procedure": ("procedure_occurrence", "procedure_occurrence_id", "procedure_concept_id", "etl_procedure_occurrence_xwalk", "procedure_occurrence_id"),
    "Specimen": ("specimen", "specimen_id", "specimen_concept_id", "etl_specimen_xwalk", "specimen_id"),
}


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return "unknown"


def _schema(value: object) -> str:
    value = str(value or "dbo")
    if not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError(f"Unsafe SQL Server schema: {value!r}")
    return value


def _columns(con, schema: str, table: str) -> set[str]:
    rows = con.execute(text("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table
    """), {"schema": schema, "table": table}).fetchall()
    return {str(r[0]).lower() for r in rows}


def _procedure_filter(cols: set[str], alias: str) -> str:
    if "source_family" in cols:
        return f"UPPER(CONVERT(varchar(64), {alias}.source_family))='PROCEDURES'"
    if "source_domain" in cols:
        return f"UPPER(CONVERT(varchar(64), {alias}.source_domain))='PROCEDURES'"
    if "source_table" in cols:
        return f"UPPER(CONVERT(varchar(64), {alias}.source_table))='PROCEDURES'"
    if "source_procedure_id" in cols:
        return f"{alias}.source_procedure_id IS NOT NULL"
    if "source_record_id" in cols and "route_id" in cols:
        return (
            f"EXISTS (SELECT 1 FROM {{route_table}} r "
            f"WHERE r.route_id={alias}.route_id AND r.source_procedure_id={alias}.source_record_id)"
        )
    if "route_id" in cols:
        return f"EXISTS (SELECT 1 FROM {{route_table}} r WHERE r.route_id={alias}.route_id)"
    raise RuntimeError(f"Cannot identify Procedure provenance from lineage columns: {sorted(cols)}")


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    target_schema = _schema(config.raw["sqlserver"].get("target_schema", "dbo"))
    out = Path(output_dir) if output_dir else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "procedure"
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            if not table_exists(con, target_schema, ROUTE_TABLE):
                raise RuntimeError(f"Missing [{target_schema}].[{ROUTE_TABLE}]")

            print("progress: materializing nonzero Procedure semantic concept space", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#procedure_concepts') IS NOT NULL DROP TABLE #procedure_concepts")
            con.exec_driver_sql(f"""
                SELECT DISTINCT target_domain, CAST(target_concept_id AS bigint) AS target_concept_id
                INTO #procedure_concepts
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE disposition='event_route' AND target_concept_id<>0;
                CREATE UNIQUE CLUSTERED INDEX IX_procedure_concepts
                  ON #procedure_concepts(target_domain,target_concept_id);
            """)

            domain_rows: list[dict[str, object]] = []
            for domain, (table, id_col, concept_col, xwalk, xwalk_id) in DOMAINS.items():
                for required in (table, xwalk):
                    if not table_exists(con, target_schema, required):
                        raise RuntimeError(f"Missing [{target_schema}].[{required}]")
                cols = _columns(con, target_schema, xwalk)
                if xwalk_id.lower() not in cols:
                    raise RuntimeError(f"{xwalk} lacks expected target id {xwalk_id}; columns={sorted(cols)}")
                provenance = _procedure_filter(cols, "x").format(route_table=f"[{target_schema}].[{ROUTE_TABLE}]")

                row = con.execute(text(f"""
                    WITH target_space AS (
                      SELECT t.{id_col} AS target_row_id, t.person_id,
                             CASE WHEN EXISTS (
                               SELECT 1 FROM [{target_schema}].[{xwalk}] x
                               WHERE x.{xwalk_id}=t.{id_col} AND {provenance}
                             ) THEN 1 ELSE 0 END AS is_procedure_derived
                      FROM [{target_schema}].[{table}] t
                      JOIN #procedure_concepts c
                        ON c.target_domain=:domain AND c.target_concept_id=t.{concept_col}
                    )
                    SELECT COUNT_BIG(*),
                           SUM(CASE WHEN is_procedure_derived=1 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN is_procedure_derived=0 THEN 1 ELSE 0 END),
                           COUNT_BIG(DISTINCT person_id),
                           COUNT_BIG(DISTINCT CASE WHEN is_procedure_derived=1 THEN person_id END),
                           COUNT_BIG(DISTINCT CASE WHEN is_procedure_derived=0 THEN person_id END)
                    FROM target_space
                """), {"domain": domain}).one()
                vals = [int(v or 0) for v in row]
                domain_rows.append({
                    "target_domain": domain,
                    "target_rows_in_procedure_concept_space": vals[0],
                    "procedure_derived_rows": vals[1],
                    "other_provenance_rows": vals[2],
                    "target_patients": vals[3],
                    "procedure_derived_patients": vals[4],
                    "other_provenance_patients": vals[5],
                    "lineage_table": xwalk,
                })
    finally:
        engine.dispose()

    total_target = sum(int(r["target_rows_in_procedure_concept_space"]) for r in domain_rows)
    procedure_derived = sum(int(r["procedure_derived_rows"]) for r in domain_rows)
    other = sum(int(r["other_provenance_rows"]) for r in domain_rows)
    summary = {
        "status": "stage_b_procedure_secondary_attribution_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git(["rev-parse", "HEAD"]),
        "analysis_worktree_clean": _git(["status", "--porcelain"]) == "",
        "method": "Secondary lineage attribution only; does not redefine the primary Procedure concordance result.",
        "totals": {
            "target_rows_in_procedure_concept_space": total_target,
            "procedure_derived_rows": procedure_derived,
            "other_provenance_rows": other,
        },
        "domain_summary": domain_rows,
    }
    (out / "stage_b_procedure_attribution.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    with (out / "procedure_target_provenance.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(domain_rows[0].keys()))
        w.writeheader(); w.writerows(domain_rows)

    print("status: stage_b_procedure_secondary_attribution_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"target_rows_in_procedure_concept_space: {total_target}")
    print(f"procedure_derived_rows: {procedure_derived}")
    print(f"other_provenance_rows: {other}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Attribute Procedure concept-space target excess using frozen lineage")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
