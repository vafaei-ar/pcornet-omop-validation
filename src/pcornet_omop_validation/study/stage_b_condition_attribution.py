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
ROUTE_TABLE = "etl_condition_event_route_v2"
PRIMARY_CONDITION_XWALK = "etl_condition_occurrence_xwalk"
CROSS_DOMAIN_XWALK = "etl_condition_cross_domain_xwalk"

DOMAINS = {
    "Condition": ("condition_occurrence", "condition_occurrence_id", "condition_concept_id", "condition_start_date"),
    "Observation": ("observation", "observation_id", "observation_concept_id", "observation_date"),
    "Procedure": ("procedure_occurrence", "procedure_occurrence_id", "procedure_concept_id", "procedure_date"),
    "Measurement": ("measurement", "measurement_id", "measurement_concept_id", "measurement_date"),
    "Drug": ("drug_exposure", "drug_exposure_id", "drug_concept_id", "drug_exposure_start_date"),
    "Device": ("device_exposure", "device_exposure_id", "device_concept_id", "device_exposure_start_date"),
    "Specimen": ("specimen", "specimen_id", "specimen_concept_id", "specimen_date"),
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


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"))
    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent
        / "publication_analysis"
        / "stage_b_patient_concordance"
        / "condition"
    )
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = [
                (target_schema, ROUTE_TABLE),
                (target_schema, PRIMARY_CONDITION_XWALK),
                (target_schema, CROSS_DOMAIN_XWALK),
                (target_schema, "person"),
            ]
            required.extend((target_schema, meta[0]) for meta in DOMAINS.values())
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            print("progress: materializing nonzero Condition semantic concept space", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#condition_concepts') IS NOT NULL DROP TABLE #condition_concepts")
            con.exec_driver_sql(f"""
                SELECT DISTINCT target_domain, CAST(target_concept_id AS bigint) AS target_concept_id
                INTO #condition_concepts
                FROM [{target_schema}].[{ROUTE_TABLE}]
                WHERE is_core_event_route = 1
                  AND target_concept_id <> 0
                  AND target_domain IN ('Condition','Observation','Procedure','Measurement','Drug','Device','Specimen');
                CREATE UNIQUE CLUSTERED INDEX IX_condition_concepts
                  ON #condition_concepts(target_domain, target_concept_id);
            """)

            print("progress: classifying native OMOP rows by Condition provenance", flush=True)
            domain_rows: list[dict[str, object]] = []
            for domain, (table, id_col, concept_col, date_col) in DOMAINS.items():
                if domain == "Condition":
                    lineage_join = f"""
                    LEFT JOIN [{target_schema}].[{PRIMARY_CONDITION_XWALK}] x
                      ON x.condition_occurrence_id = t.{id_col}
                    """
                    condition_flag = "CASE WHEN x.condition_occurrence_id IS NOT NULL THEN 1 ELSE 0 END"
                else:
                    lineage_join = f"""
                    LEFT JOIN [{target_schema}].[{CROSS_DOMAIN_XWALK}] x
                      ON x.target_domain = '{domain}'
                     AND x.target_row_id = t.{id_col}
                    """
                    condition_flag = "CASE WHEN x.target_row_id IS NOT NULL THEN 1 ELSE 0 END"

                row = con.execute(text(f"""
                    WITH target_space AS (
                      SELECT
                        t.{id_col} AS target_row_id,
                        t.person_id,
                        CAST(t.{date_col} AS date) AS event_date,
                        CAST(t.{concept_col} AS bigint) AS target_concept_id,
                        {condition_flag} AS is_condition_derived
                      FROM [{target_schema}].[{table}] t
                      JOIN #condition_concepts c
                        ON c.target_domain = '{domain}'
                       AND c.target_concept_id = t.{concept_col}
                      {lineage_join}
                    )
                    SELECT
                      COUNT_BIG(*) AS target_rows,
                      SUM(CASE WHEN is_condition_derived=1 THEN 1 ELSE 0 END) AS condition_derived_rows,
                      SUM(CASE WHEN is_condition_derived=0 THEN 1 ELSE 0 END) AS other_provenance_rows,
                      COUNT_BIG(DISTINCT person_id) AS target_patients,
                      COUNT_BIG(DISTINCT CASE WHEN is_condition_derived=1 THEN person_id END) AS condition_derived_patients,
                      COUNT_BIG(DISTINCT CASE WHEN is_condition_derived=0 THEN person_id END) AS other_provenance_patients
                    FROM target_space
                """)).one()
                vals = [int(v or 0) for v in row]
                domain_rows.append({
                    "target_domain": domain,
                    "target_rows_in_condition_concept_space": vals[0],
                    "condition_derived_rows": vals[1],
                    "other_provenance_rows": vals[2],
                    "target_patients": vals[3],
                    "condition_derived_patients": vals[4],
                    "other_provenance_patients": vals[5],
                })

            print("progress: checking target-only patient attribution", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#source_condition_patients') IS NOT NULL DROP TABLE #source_condition_patients")
            con.exec_driver_sql(f"""
                SELECT DISTINCT p.person_id
                INTO #source_condition_patients
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{target_schema}].[{PRIMARY_CONDITION_XWALK}] x
                  ON x.route_id = r.route_id
                JOIN [{target_schema}].[condition_occurrence] co
                  ON co.condition_occurrence_id = x.condition_occurrence_id
                JOIN [{target_schema}].[person] p ON p.person_id = co.person_id
                WHERE r.is_core_event_route=1
                  AND r.target_concept_id<>0
                  AND r.target_domain='Condition'
                UNION
                SELECT DISTINCT t.person_id
                FROM [{target_schema}].[{CROSS_DOMAIN_XWALK}] x
                JOIN [{target_schema}].[{ROUTE_TABLE}] r ON r.route_id=x.route_id
                JOIN (
                    SELECT 'Observation' AS target_domain, observation_id AS target_row_id, person_id FROM [{target_schema}].[observation]
                    UNION ALL SELECT 'Procedure', procedure_occurrence_id, person_id FROM [{target_schema}].[procedure_occurrence]
                    UNION ALL SELECT 'Measurement', measurement_id, person_id FROM [{target_schema}].[measurement]
                    UNION ALL SELECT 'Drug', drug_exposure_id, person_id FROM [{target_schema}].[drug_exposure]
                    UNION ALL SELECT 'Device', device_exposure_id, person_id FROM [{target_schema}].[device_exposure]
                    UNION ALL SELECT 'Specimen', specimen_id, person_id FROM [{target_schema}].[specimen]
                ) t
                  ON t.target_domain=x.target_domain AND t.target_row_id=x.target_row_id
                WHERE r.is_core_event_route=1 AND r.target_concept_id<>0;
                CREATE UNIQUE CLUSTERED INDEX IX_source_condition_patients ON #source_condition_patients(person_id);
            """)

            union_parts = []
            for domain, (table, id_col, concept_col, _) in DOMAINS.items():
                union_parts.append(f"""
                    SELECT DISTINCT t.person_id
                    FROM [{target_schema}].[{table}] t
                    JOIN #condition_concepts c
                      ON c.target_domain='{domain}' AND c.target_concept_id=t.{concept_col}
                """)
            target_patient_union_sql = " UNION ".join(union_parts)
            target_only_row = con.execute(text(f"""
                WITH tp AS ({target_patient_union_sql}),
                target_only AS (
                  SELECT tp.person_id
                  FROM tp
                  LEFT JOIN #source_condition_patients sp ON sp.person_id=tp.person_id
                  WHERE sp.person_id IS NULL
                )
                SELECT COUNT_BIG(*) FROM target_only
            """)).one()
            target_only_patients = int(target_only_row[0] or 0)

    finally:
        engine.dispose()

    total_target = sum(int(r["target_rows_in_condition_concept_space"]) for r in domain_rows)
    condition_derived = sum(int(r["condition_derived_rows"]) for r in domain_rows)
    other_provenance = sum(int(r["other_provenance_rows"]) for r in domain_rows)

    summary = {
        "status": "stage_b_condition_secondary_attribution_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git(["rev-parse", "HEAD"]),
        "analysis_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "analysis_worktree_clean": _git(["status", "--porcelain"]) == "",
        "method": (
            "Secondary attribution only. Native OMOP rows in the prespecified Condition concept space are classified by frozen Condition-specific lineage. "
            "This does not redefine or replace the primary Stage B Condition comparison."
        ),
        "totals": {
            "target_rows_in_condition_concept_space": total_target,
            "condition_derived_rows": condition_derived,
            "other_provenance_rows": other_provenance,
            "target_only_patients_relative_to_condition_derived_patient_set": target_only_patients,
        },
        "domain_summary": domain_rows,
        "interpretation": [
            "Condition-derived rows are identified only through frozen Condition primary/cross-domain lineage after the primary native-CDM comparison.",
            "Rows in the same OMOP concept space without Condition lineage are classified as other provenance, not as failed Condition transformation.",
            "The frozen global reconciliation remains the authoritative evidence that target rows are explained by the audited pipeline; this module only partitions Condition versus non-Condition provenance.",
        ],
    }

    (out / "stage_b_condition_attribution.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (out / "condition_target_provenance.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(domain_rows[0].keys()))
        w.writeheader(); w.writerows(domain_rows)

    print("status: stage_b_condition_secondary_attribution_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"target_rows_in_condition_concept_space: {total_target}")
    print(f"condition_derived_rows: {condition_derived}")
    print(f"other_provenance_rows: {other_provenance}")
    print(f"target_only_patients_relative_to_condition_derived_patient_set: {target_only_patients}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Attribute Stage B Condition target extras using frozen lineage")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
