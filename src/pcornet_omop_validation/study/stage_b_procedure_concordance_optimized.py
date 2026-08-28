from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_b_v1.json")
ROUTE_TABLE = "etl_procedure_event_route"
EVENT_DOMAINS = ("Condition", "Device", "Drug", "Measurement", "Observation", "Procedure", "Specimen")
TARGETS = {
    "Condition": ("condition_occurrence", "condition_concept_id", "condition_start_date"),
    "Device": ("device_exposure", "device_concept_id", "device_exposure_start_date"),
    "Drug": ("drug_exposure", "drug_concept_id", "drug_exposure_start_date"),
    "Measurement": ("measurement", "measurement_concept_id", "measurement_date"),
    "Observation": ("observation", "observation_concept_id", "observation_date"),
    "Procedure": ("procedure_occurrence", "procedure_concept_id", "procedure_date"),
    "Specimen": ("specimen", "specimen_concept_id", "specimen_date"),
}


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema(value: object) -> str:
    value = str(value or "dbo")
    if not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError(f"Unsafe SQL Server schema: {value!r}")
    return value


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _jaccard(intersection: int, union: int) -> float | None:
    return None if union == 0 else intersection / union


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"))
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"))

    study_path = Path(STUDY_DEFINITION)
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-v1" or study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage B study definition is not the locked frozen-ETL definition")

    out = Path(output_dir) if output_dir else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "procedure"
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = [(target_schema, ROUTE_TABLE), (target_schema, "person")]
            required.extend((target_schema, TARGETS[d][0]) for d in EVENT_DOMAINS)
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            print("progress: materializing Procedure source semantic routes", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#procedure_source') IS NOT NULL DROP TABLE #procedure_source")
            con.exec_driver_sql(f"""
                SELECT
                    p.person_id,
                    CAST(r.px_date AS date) AS event_date,
                    r.target_domain,
                    CAST(r.target_concept_id AS bigint) AS target_concept_id,
                    r.source_procedure_id,
                    r.route_status,
                    r.disposition
                INTO #procedure_source
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value = CONVERT(nvarchar(50), r.patid);
                CREATE INDEX IX_procedure_source_event
                  ON #procedure_source(target_domain, target_concept_id, person_id, event_date);
                CREATE INDEX IX_procedure_source_id
                  ON #procedure_source(source_procedure_id);
            """)

            print("progress: materializing mapped event routes and concept space", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#procedure_mapped') IS NOT NULL DROP TABLE #procedure_mapped")
            con.exec_driver_sql("""
                SELECT person_id, event_date, target_domain, target_concept_id, source_procedure_id
                INTO #procedure_mapped
                FROM #procedure_source
                WHERE disposition='event_route' AND target_concept_id<>0;
                CREATE INDEX IX_procedure_mapped_sig
                  ON #procedure_mapped(target_domain, target_concept_id, person_id, event_date);
            """)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#procedure_concepts') IS NOT NULL DROP TABLE #procedure_concepts")
            con.exec_driver_sql("""
                SELECT DISTINCT target_domain, target_concept_id
                INTO #procedure_concepts
                FROM #procedure_mapped;
                CREATE UNIQUE CLUSTERED INDEX IX_procedure_concepts
                  ON #procedure_concepts(target_domain, target_concept_id);
            """)

            print("progress: scanning native OMOP event tables once into Procedure concept space", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#procedure_target') IS NOT NULL DROP TABLE #procedure_target")
            union_parts = []
            for domain in EVENT_DOMAINS:
                table, concept_col, date_col = TARGETS[domain]
                union_parts.append(f"""
                    SELECT t.person_id, CAST(t.{date_col} AS date) AS event_date,
                           CAST('{domain}' AS varchar(32)) AS target_domain,
                           CAST(t.{concept_col} AS bigint) AS target_concept_id
                    FROM [{target_schema}].[{table}] t
                    JOIN #procedure_concepts c
                      ON c.target_domain='{domain}' AND c.target_concept_id=t.{concept_col}
                """)
            con.exec_driver_sql(" SELECT person_id,event_date,target_domain,target_concept_id INTO #procedure_target FROM (" + " UNION ALL ".join(union_parts) + ") q")
            con.exec_driver_sql("CREATE INDEX IX_procedure_target_sig ON #procedure_target(target_domain,target_concept_id,person_id,event_date)")

            print("progress: aggregating semantic signatures", flush=True)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#s_sig') IS NOT NULL DROP TABLE #s_sig")
            con.exec_driver_sql("""
                SELECT person_id,event_date,target_domain,target_concept_id,COUNT_BIG(*) AS n
                INTO #s_sig FROM #procedure_mapped
                GROUP BY person_id,event_date,target_domain,target_concept_id;
                CREATE UNIQUE CLUSTERED INDEX IX_s_sig
                  ON #s_sig(person_id,event_date,target_domain,target_concept_id);
            """)
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#t_sig') IS NOT NULL DROP TABLE #t_sig")
            con.exec_driver_sql("""
                SELECT person_id,event_date,target_domain,target_concept_id,COUNT_BIG(*) AS n
                INTO #t_sig FROM #procedure_target
                GROUP BY person_id,event_date,target_domain,target_concept_id;
                CREATE UNIQUE CLUSTERED INDEX IX_t_sig
                  ON #t_sig(person_id,event_date,target_domain,target_concept_id);
            """)

            print("progress: computing aggregate concordance metrics", flush=True)
            overview = con.execute(text("""
                SELECT
                  (SELECT COUNT_BIG(DISTINCT source_procedure_id) FROM #procedure_source),
                  (SELECT COUNT_BIG(*) FROM #procedure_source),
                  (SELECT COUNT_BIG(*) FROM #procedure_mapped),
                  (SELECT COUNT_BIG(*) FROM #procedure_source WHERE disposition='unresolved'),
                  (SELECT COUNT_BIG(*) FROM #procedure_source WHERE disposition='non_event_semantic_component'),
                  (SELECT COUNT_BIG(*) FROM #procedure_target),
                  (SELECT COUNT_BIG(DISTINCT person_id) FROM #procedure_mapped),
                  (SELECT COUNT_BIG(DISTINCT person_id) FROM #procedure_target),
                  (SELECT COUNT_BIG(*) FROM (
                     SELECT source_procedure_id FROM #procedure_mapped
                     GROUP BY source_procedure_id HAVING COUNT_BIG(*)>1
                   ) q)
            """)).one()
            (source_events, source_route_rows, source_mapped_route_rows, unresolved_rows,
             non_event_rows, target_semantic_rows, source_patients, target_patients,
             multi_route_source_events) = map(int, overview)

            patient = con.execute(text("""
                WITH s AS (SELECT DISTINCT person_id FROM #procedure_mapped),
                     t AS (SELECT DISTINCT person_id FROM #procedure_target),
                     a AS (SELECT person_id FROM s UNION SELECT person_id FROM t)
                SELECT
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END)
                FROM a LEFT JOIN s ON s.person_id=a.person_id LEFT JOIN t ON t.person_id=a.person_id
            """)).one()
            intersection, source_only, target_only = map(int, patient)
            patient_union = intersection + source_only + target_only

            sig = con.execute(text("""
                WITH k AS (
                  SELECT person_id,event_date,target_domain,target_concept_id FROM #s_sig
                  UNION SELECT person_id,event_date,target_domain,target_concept_id FROM #t_sig
                )
                SELECT
                  SUM(CASE WHEN COALESCE(s.n,0)<COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),
                  SUM(CASE WHEN COALESCE(s.n,0)>COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),
                  SUM(CASE WHEN COALESCE(t.n,0)>COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END)
                FROM k
                LEFT JOIN #s_sig s ON s.person_id=k.person_id AND s.event_date=k.event_date AND s.target_domain=k.target_domain AND s.target_concept_id=k.target_concept_id
                LEFT JOIN #t_sig t ON t.person_id=k.person_id AND t.event_date=k.event_date AND t.target_domain=k.target_domain AND t.target_concept_id=k.target_concept_id
            """)).one()
            matched, source_unmatched, target_unmatched = map(int, sig)

            domain_rows = []
            for domain in EVENT_DOMAINS:
                row = con.execute(text("""
                    WITH s AS (SELECT * FROM #s_sig WHERE target_domain=:domain),
                         t AS (SELECT * FROM #t_sig WHERE target_domain=:domain),
                         k AS (
                           SELECT person_id,event_date,target_concept_id FROM s
                           UNION SELECT person_id,event_date,target_concept_id FROM t
                         )
                    SELECT
                      COALESCE((SELECT SUM(n) FROM s),0),
                      COALESCE((SELECT SUM(n) FROM t),0),
                      COALESCE((SELECT COUNT_BIG(DISTINCT person_id) FROM s),0),
                      COALESCE((SELECT COUNT_BIG(DISTINCT person_id) FROM t),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0)<COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0)>COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(t.n,0)>COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END),0)
                    FROM k
                    LEFT JOIN s ON s.person_id=k.person_id AND s.event_date=k.event_date AND s.target_concept_id=k.target_concept_id
                    LEFT JOIN t ON t.person_id=k.person_id AND t.event_date=k.event_date AND t.target_concept_id=k.target_concept_id
                """), {"domain": domain}).one()
                sr, tr, sp, tp, m, su, tu = [int(v or 0) for v in row]
                domain_rows.append({
                    "target_domain": domain,
                    "source_mapped_route_rows": sr,
                    "target_native_rows_in_source_concept_space": tr,
                    "source_patients": sp,
                    "target_patients": tp,
                    "exact_signature_matched_events": m,
                    "source_unmatched_events": su,
                    "target_unmatched_events": tu,
                    "source_match_percent": _pct(m, sr),
                    "target_match_percent": _pct(m, tr),
                })
    finally:
        engine.dispose()

    summary = {
        "status": "stage_b_procedure_concordance_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_definition": "stage-b-v1",
        "study_definition_sha256": _sha256(study_path),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": _git(["rev-parse", "HEAD"]),
        "analysis_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "analysis_worktree_clean": _git(["status", "--porcelain"]) == "",
        "database": sql_cfg.get("database"),
        "source_schema": source_schema,
        "target_schema": target_schema,
        "primary_comparison": {
            "source_eligible_events_represented_in_route_ledger": source_events,
            "source_route_rows_all_dispositions": source_route_rows,
            "source_mapped_event_route_rows": source_mapped_route_rows,
            "source_unresolved_route_rows": unresolved_rows,
            "source_non_event_semantic_component_rows": non_event_rows,
            "source_multi_event_route_events": multi_route_source_events,
            "target_native_rows_in_source_concept_space": target_semantic_rows,
            "source_mapped_patients": source_patients,
            "target_mapped_patients": target_patients,
            "intersection_patients": intersection,
            "source_only_patients": source_only,
            "target_only_patients": target_only,
            "union_patients": patient_union,
            "patient_jaccard": _jaccard(intersection, patient_union),
            "patient_positive_agreement_percent": _pct(2 * intersection, source_patients + target_patients),
            "exact_person_date_domain_concept_matched_events": matched,
            "source_unmatched_signature_events": source_unmatched,
            "target_unmatched_signature_events": target_unmatched,
            "source_exact_signature_match_percent": _pct(matched, source_mapped_route_rows),
            "target_exact_signature_match_percent": _pct(matched, target_semantic_rows),
        },
        "domain_summary": domain_rows,
        "interpretation_rules": [
            "Only nonzero Procedure event routes enter mapped semantic concordance denominators.",
            "Unresolved concept-0 routes and non-event semantic components are reported separately.",
            "One-to-many Standard event routes are preserved rather than collapsed.",
            "Cross-domain Procedure routes are compared in their native OMOP target domains.",
            "Target lineage/xwalk tables are not used in the primary comparison and may be used later only for attribution.",
        ],
    }

    (out / "stage_b_procedure_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    with (out / "procedure_domain_concordance.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(domain_rows[0].keys()))
        w.writeheader(); w.writerows(domain_rows)

    print("status: stage_b_procedure_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_events: {source_events}")
    print(f"source_route_rows: {source_route_rows}")
    print(f"source_mapped_event_route_rows: {source_mapped_route_rows}")
    print(f"unresolved_rows: {unresolved_rows}")
    print(f"non_event_semantic_component_rows: {non_event_rows}")
    print(f"multi_event_route_source_events: {multi_route_source_events}")
    print(f"source_mapped_patients: {source_patients}")
    print(f"target_mapped_patients: {target_patients}")
    print(f"intersection_patients: {intersection}")
    print(f"source_only_patients: {source_only}")
    print(f"target_only_patients: {target_only}")
    print(f"patient_jaccard: {summary['primary_comparison']['patient_jaccard']}")
    print(f"exact_signature_matched_events: {matched}")
    print(f"source_unmatched_signature_events: {source_unmatched}")
    print(f"target_unmatched_signature_events: {target_unmatched}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run optimized Stage B Wave 1 Procedure semantic concordance")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
