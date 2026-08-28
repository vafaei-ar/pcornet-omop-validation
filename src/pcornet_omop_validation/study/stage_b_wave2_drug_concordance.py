from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_DEFINITION = Path("study_definitions/stage_b_wave2_v1.json")
ROUTE_TABLE = "etl_drug_event_route"


def _git(*args: str) -> str:
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


def _schema(value: object, label: str) -> str:
    s = str(value or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe SQL Server {label}: {s!r}")
    return s


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _jaccard(intersection: int, union: int) -> float | None:
    return None if union == 0 else intersection / union


def _progress(message: str) -> None:
    print(f"progress: {message}", flush=True)


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    config = load_etl_config(config_path)
    study_path = Path(STUDY_DEFINITION)
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-wave2-v1":
        raise RuntimeError("Drug concordance requires locked stage-b-wave2-v1 definition")
    if study.get("status") != "prespecified_before_wave2_outcome_queries":
        raise RuntimeError("Wave 2 definition is not prespecified before outcome queries")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Wave 2 definition frozen SHA mismatch")

    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "drug"
    )
    out.mkdir(parents=True, exist_ok=True)

    required = (
        (source_schema, "PCORnet_PRESCRIBING"),
        (source_schema, "PCORnet_DISPENSING"),
        (source_schema, "PCORnet_MED_ADMIN"),
        (source_schema, "PCORnet_IMMUNIZATION"),
        (source_schema, "PCORnet_PROCEDURES"),
        (target_schema, "person"),
        (target_schema, ROUTE_TABLE),
        (target_schema, "drug_exposure"),
        (target_schema, "concept"),
    )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            invalid_nonzero = _scalar(con, f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id=r.target_concept_id
                WHERE COALESCE(r.target_concept_id,0)<>0
                  AND (c.concept_id IS NULL OR c.domain_id<>'Drug'
                       OR c.standard_concept<>'S' OR c.invalid_reason IS NOT NULL)
            """)
            if invalid_nonzero:
                raise RuntimeError(f"Drug route ledger contains {invalid_nonzero} invalid nonzero targets")

            _progress("materializing source Drug semantic routes")
            con.exec_driver_sql("SET NOCOUNT ON")
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#drug_source') IS NOT NULL DROP TABLE #drug_source")
            con.exec_driver_sql(f"""
                SELECT p.person_id,
                       CAST(COALESCE(src.RX_START_DATE,src.RX_ORDER_DATE) AS date) AS event_date,
                       CAST(r.target_concept_id AS bigint) AS target_concept_id,
                       CAST(r.source_domain AS varchar(32)) AS source_family,
                       CAST(r.source_record_id AS nvarchar(255)) AS source_record_id,
                       CAST(r.mapping_basis AS varchar(128)) AS mapping_basis,
                       CAST(r.disposition AS varchar(64)) AS disposition
                INTO #drug_source
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{source_schema}].[PCORnet_PRESCRIBING] src
                  ON r.source_domain='PRESCRIBING'
                 AND r.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PRESCRIBINGID)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PATID)))
                WHERE COALESCE(src.RX_START_DATE,src.RX_ORDER_DATE) IS NOT NULL

                UNION ALL

                SELECT p.person_id,CAST(src.DISPENSE_DATE AS date),CAST(r.target_concept_id AS bigint),
                       CAST(r.source_domain AS varchar(32)),CAST(r.source_record_id AS nvarchar(255)),
                       CAST(r.mapping_basis AS varchar(128)),CAST(r.disposition AS varchar(64))
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{source_schema}].[PCORnet_DISPENSING] src
                  ON r.source_domain='DISPENSING'
                 AND r.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),src.DISPENSINGID)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PATID)))
                WHERE src.DISPENSE_DATE IS NOT NULL

                UNION ALL

                SELECT p.person_id,CAST(src.MEDADMIN_START_DATE AS date),CAST(r.target_concept_id AS bigint),
                       CAST(r.source_domain AS varchar(32)),CAST(r.source_record_id AS nvarchar(255)),
                       CAST(r.mapping_basis AS varchar(128)),CAST(r.disposition AS varchar(64))
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{source_schema}].[PCORnet_MED_ADMIN] src
                  ON r.source_domain='MED_ADMIN'
                 AND r.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),src.MEDADMINID)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PATID)))
                WHERE src.MEDADMIN_START_DATE IS NOT NULL

                UNION ALL

                SELECT p.person_id,CAST(src.VX_ADMIN_DATE AS date),CAST(r.target_concept_id AS bigint),
                       CAST(r.source_domain AS varchar(32)),CAST(r.source_record_id AS nvarchar(255)),
                       CAST(r.mapping_basis AS varchar(128)),CAST(r.disposition AS varchar(64))
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{source_schema}].[PCORnet_IMMUNIZATION] src
                  ON r.source_domain='IMMUNIZATION'
                 AND r.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),src.IMMUNIZATIONID)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PATID)))
                WHERE src.VX_ADMIN_DATE IS NOT NULL

                UNION ALL

                SELECT p.person_id,CAST(src.PX_DATE AS date),CAST(r.target_concept_id AS bigint),
                       CAST(r.source_domain AS varchar(32)),CAST(r.source_record_id AS nvarchar(255)),
                       CAST(r.mapping_basis AS varchar(128)),CAST(r.disposition AS varchar(64))
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                JOIN [{source_schema}].[PCORnet_PROCEDURES] src
                  ON r.source_domain='PROCEDURES'
                 AND r.source_record_id=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PROCEDURESID)))
                JOIN [{target_schema}].[person] p
                  ON p.person_source_value=LTRIM(RTRIM(CONVERT(nvarchar(255),src.PATID)))
                WHERE src.PX_DATE IS NOT NULL;

                CREATE INDEX IX_drug_source_sig
                  ON #drug_source(target_concept_id,person_id,event_date);
                CREATE INDEX IX_drug_source_event
                  ON #drug_source(source_family,source_record_id);
            """)

            route_rows = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}]")
            source_rows = _scalar(con, "SELECT COUNT_BIG(*) FROM #drug_source")
            if source_rows != route_rows:
                raise RuntimeError(
                    f"Source semantic materialization did not reproduce Drug route ledger: {source_rows} vs {route_rows}"
                )

            _progress("materializing mapped Drug routes and native OMOP concept space")
            con.exec_driver_sql("IF OBJECT_ID('tempdb..#drug_mapped') IS NOT NULL DROP TABLE #drug_mapped")
            con.exec_driver_sql("""
                SELECT person_id,event_date,target_concept_id,source_family,source_record_id
                INTO #drug_mapped
                FROM #drug_source
                WHERE target_concept_id<>0;
                CREATE INDEX IX_drug_mapped_sig
                  ON #drug_mapped(target_concept_id,person_id,event_date);

                SELECT DISTINCT target_concept_id
                INTO #drug_concepts
                FROM #drug_mapped;
                CREATE UNIQUE CLUSTERED INDEX IX_drug_concepts ON #drug_concepts(target_concept_id);
            """)

            con.exec_driver_sql("IF OBJECT_ID('tempdb..#drug_target') IS NOT NULL DROP TABLE #drug_target")
            con.exec_driver_sql(f"""
                SELECT d.drug_exposure_id,d.person_id,
                       CAST(d.drug_exposure_start_date AS date) AS event_date,
                       CAST(d.drug_concept_id AS bigint) AS target_concept_id,
                       d.drug_type_concept_id,d.route_concept_id,d.visit_occurrence_id
                INTO #drug_target
                FROM [{target_schema}].[drug_exposure] d
                JOIN #drug_concepts c ON c.target_concept_id=d.drug_concept_id;
                CREATE INDEX IX_drug_target_sig
                  ON #drug_target(target_concept_id,person_id,event_date);
            """)

            _progress("aggregating Drug semantic signatures")
            con.exec_driver_sql("""
                SELECT person_id,event_date,target_concept_id,COUNT_BIG(*) AS n
                INTO #s_sig FROM #drug_mapped
                GROUP BY person_id,event_date,target_concept_id;
                CREATE UNIQUE CLUSTERED INDEX IX_s_sig
                  ON #s_sig(person_id,event_date,target_concept_id);

                SELECT person_id,event_date,target_concept_id,COUNT_BIG(*) AS n
                INTO #t_sig FROM #drug_target
                GROUP BY person_id,event_date,target_concept_id;
                CREATE UNIQUE CLUSTERED INDEX IX_t_sig
                  ON #t_sig(person_id,event_date,target_concept_id);
            """)

            _progress("computing Drug concordance and secondary characterization")
            source_mapped = _scalar(con, "SELECT COUNT_BIG(*) FROM #drug_mapped")
            unresolved = _scalar(con, "SELECT COUNT_BIG(*) FROM #drug_source WHERE target_concept_id=0")
            target_space_rows = _scalar(con, "SELECT COUNT_BIG(*) FROM #drug_target")

            source_events = _scalar(con, """
                SELECT COUNT_BIG(*) FROM (
                  SELECT source_family,source_record_id FROM #drug_source
                  GROUP BY source_family,source_record_id
                ) q
            """)
            multi_route_events = _scalar(con, """
                SELECT COUNT_BIG(*) FROM (
                  SELECT source_family,source_record_id,COUNT_BIG(*) n
                  FROM #drug_mapped GROUP BY source_family,source_record_id
                  HAVING COUNT_BIG(*)>1
                ) q
            """)

            patient_row = con.execute(text("""
                WITH s AS (SELECT DISTINCT person_id FROM #drug_mapped),
                     t AS (SELECT DISTINCT person_id FROM #drug_target),
                     a AS (SELECT person_id FROM s UNION SELECT person_id FROM t)
                SELECT
                  SUM(CASE WHEN s.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END)
                FROM a LEFT JOIN s ON s.person_id=a.person_id LEFT JOIN t ON t.person_id=a.person_id
            """)).one()
            source_patients,target_patients,intersection,source_only,target_only = [int(v or 0) for v in patient_row]
            patient_union = intersection + source_only + target_only

            sig_row = con.execute(text("""
                SELECT
                  SUM(CASE WHEN COALESCE(s.n,0)<COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),
                  SUM(CASE WHEN COALESCE(s.n,0)>COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),
                  SUM(CASE WHEN COALESCE(t.n,0)>COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END)
                FROM #s_sig s
                FULL OUTER JOIN #t_sig t
                  ON t.person_id=s.person_id AND t.event_date=s.event_date
                 AND t.target_concept_id=s.target_concept_id
            """)).one()
            matched,source_unmatched,target_unmatched = [int(v or 0) for v in sig_row]

            family_rows = [dict(r) for r in con.execute(text("""
                SELECT source_family,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN target_concept_id<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN target_concept_id=0 THEN 1 ELSE 0 END) AS unresolved_rows,
                       COUNT_BIG(DISTINCT source_record_id) AS distinct_source_events,
                       COUNT_BIG(DISTINCT CASE WHEN target_concept_id<>0 THEN person_id END) AS mapped_patients
                FROM #drug_source
                GROUP BY source_family ORDER BY source_family
            """)).mappings().all()]

            mapping_rows = [dict(r) for r in con.execute(text("""
                SELECT source_family,mapping_basis,disposition,
                       COUNT_BIG(*) AS route_rows,
                       SUM(CASE WHEN target_concept_id<>0 THEN 1 ELSE 0 END) AS mapped_rows,
                       SUM(CASE WHEN target_concept_id=0 THEN 1 ELSE 0 END) AS unresolved_rows
                FROM #drug_source
                GROUP BY source_family,mapping_basis,disposition
                ORDER BY source_family,mapping_basis,disposition
            """)).mappings().all()]

            target_secondary = con.execute(text("""
                SELECT COUNT_BIG(*) AS target_rows,
                       SUM(CASE WHEN drug_type_concept_id=0 THEN 1 ELSE 0 END) AS type_zero_rows,
                       SUM(CASE WHEN drug_type_concept_id<>0 THEN 1 ELSE 0 END) AS type_nonzero_rows,
                       SUM(CASE WHEN route_concept_id=0 THEN 1 ELSE 0 END) AS route_zero_rows,
                       SUM(CASE WHEN route_concept_id<>0 THEN 1 ELSE 0 END) AS route_nonzero_rows,
                       SUM(CASE WHEN visit_occurrence_id IS NOT NULL THEN 1 ELSE 0 END) AS visit_linked_rows
                FROM #drug_target
            """)).mappings().one()
            target_secondary = {k:int(v or 0) for k,v in dict(target_secondary).items()}

            type_rows = [dict(r) for r in con.execute(text("""
                SELECT drug_type_concept_id,COUNT_BIG(*) AS rows,
                       COUNT_BIG(DISTINCT person_id) AS patients
                FROM #drug_target
                GROUP BY drug_type_concept_id ORDER BY drug_type_concept_id
            """)).mappings().all()]
    finally:
        engine.dispose()

    primary = {
        "source_distinct_events": source_events,
        "source_route_rows": route_rows,
        "source_mapped_route_rows": source_mapped,
        "source_unresolved_route_rows": unresolved,
        "source_multi_mapped_route_events": multi_route_events,
        "target_native_rows_in_source_concept_space": target_space_rows,
        "source_mapped_patients": source_patients,
        "target_mapped_patients": target_patients,
        "intersection_patients": intersection,
        "source_only_patients": source_only,
        "target_only_patients": target_only,
        "union_patients": patient_union,
        "patient_jaccard": _jaccard(intersection,patient_union),
        "patient_positive_agreement_percent": _pct(2*intersection,source_patients+target_patients),
        "exact_person_date_concept_matched_events": matched,
        "source_unmatched_signature_events": source_unmatched,
        "target_unmatched_signature_events": target_unmatched,
        "source_exact_signature_match_percent": _pct(matched,source_mapped),
        "target_exact_signature_match_percent": _pct(matched,target_space_rows),
    }
    summary = {
        "status":"stage_b_wave2_drug_concordance_complete",
        "recorded_at_utc":datetime.now(timezone.utc).isoformat(),
        "study_definition":"stage-b-wave2-v1",
        "study_definition_sha256":_sha256(study_path),
        "frozen_etl_sha":FROZEN_ETL_SHA,
        "analysis_git_sha":_git("rev-parse","HEAD"),
        "analysis_branch":_git("rev-parse","--abbrev-ref","HEAD"),
        "analysis_worktree_clean":_git("status","--porcelain")=="",
        "primary_comparison":primary,
        "source_family_summary":family_rows,
        "mapping_basis_summary":mapping_rows,
        "target_secondary_characterization":target_secondary,
        "target_type_concept_summary":type_rows,
        "interpretation_rules":[
            "Only nonzero Standard Drug targets enter primary mapped semantic concordance.",
            "Concept-zero Drug routes remain unresolved coverage and are reported separately.",
            "Primary event identity is person + calendar start date + Standard Drug concept.",
            "Route concept, visit linkage, and Drug type are secondary characterization only.",
            "Target-native excess in the same concept space requires provenance attribution before being labeled discordant.",
        ],
    }

    (out/"stage_b_wave2_drug_summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True,default=str),encoding="utf-8")
    for filename,rows in (
        ("drug_source_family_summary.csv",family_rows),
        ("drug_mapping_basis_summary.csv",mapping_rows),
        ("drug_target_type_concept_summary.csv",type_rows),
    ):
        if rows:
            with (out/filename).open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0].keys()))
                w.writeheader(); w.writerows(rows)

    print("status: stage_b_wave2_drug_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_events: {source_events}")
    print(f"source_route_rows: {route_rows}")
    print(f"source_mapped_route_rows: {source_mapped}")
    print(f"source_unresolved_route_rows: {unresolved}")
    print(f"source_multi_mapped_route_events: {multi_route_events}")
    print(f"source_mapped_patients: {source_patients}")
    print(f"target_mapped_patients: {target_patients}")
    print(f"intersection_patients: {intersection}")
    print(f"source_only_patients: {source_only}")
    print(f"target_only_patients: {target_only}")
    print(f"patient_jaccard: {primary['patient_jaccard']}")
    print(f"exact_signature_matched_events: {matched}")
    print(f"source_unmatched_signature_events: {source_unmatched}")
    print(f"target_unmatched_signature_events: {target_unmatched}")
    print(f"target_type_zero_rows_in_source_concept_space: {target_secondary['type_zero_rows']}")
    print(f"target_route_zero_rows_in_source_concept_space: {target_secondary['route_zero_rows']}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser=argparse.ArgumentParser(description="Stage B Wave 2 Drug semantic concordance and characterization")
    parser.add_argument("--config",required=True)
    parser.add_argument("--output-dir")
    args=parser.parse_args()
    run(args.config,args.output_dir)


if __name__=="__main__":
    main()
