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
ROUTE_TABLE = "etl_condition_event_route_v2"
EVENT_DOMAINS = (
    "Condition",
    "Observation",
    "Procedure",
    "Measurement",
    "Drug",
    "Device",
    "Specimen",
)


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


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one())


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else 100.0 * num / den


def _jaccard(intersection: int, union: int) -> float | None:
    return None if union == 0 else intersection / union


def _semantic_ctes(source_schema: str, target_schema: str) -> str:
    """Prespecified semantic source signatures and native OMOP target signatures.

    The frozen Condition route ledger is used only as the prespecified vocabulary/domain
    semantic reference for source DIAGNOSIS/CONDITION records. Target rows are read
    independently from native OMOP clinical tables; no target xwalk/lineage table is used.
    """
    return f"""
    WITH source_semantic AS (
      SELECT
        p.person_id,
        CAST(d.DX_DATE AS date) AS event_date,
        r.target_domain,
        CAST(r.target_concept_id AS bigint) AS target_concept_id,
        r.source_domain,
        r.source_record_id,
        r.route_status,
        r.is_fallback
      FROM [{target_schema}].[{ROUTE_TABLE}] r
      JOIN [{source_schema}].[PCORnet_DIAGNOSIS] d
        ON r.source_domain = 'DIAGNOSIS'
       AND r.source_record_id = CONVERT(nvarchar(255), d.DIAGNOSISID)
      JOIN [{target_schema}].[person] p
        ON CONVERT(nvarchar(50), d.PATID) = p.person_source_value
      WHERE r.is_core_event_route = 1

      UNION ALL

      SELECT
        p.person_id,
        CAST(COALESCE(c.ONSET_DATE, c.REPORT_DATE) AS date) AS event_date,
        r.target_domain,
        CAST(r.target_concept_id AS bigint) AS target_concept_id,
        r.source_domain,
        r.source_record_id,
        r.route_status,
        r.is_fallback
      FROM [{target_schema}].[{ROUTE_TABLE}] r
      JOIN [{source_schema}].[PCORnet_CONDITION] c
        ON r.source_domain = 'CONDITION'
       AND r.source_record_id = CONVERT(nvarchar(255), c.CONDITIONID)
      JOIN [{target_schema}].[person] p
        ON CONVERT(nvarchar(50), c.PATID) = p.person_source_value
      WHERE r.is_core_event_route = 1
    ),
    source_mapped AS (
      SELECT * FROM source_semantic WHERE target_concept_id <> 0
    ),
    source_concepts AS (
      SELECT DISTINCT target_domain, target_concept_id
      FROM source_mapped
    ),
    target_semantic AS (
      SELECT o.person_id, CAST(o.condition_start_date AS date) AS event_date,
             CAST('Condition' AS varchar(32)) AS target_domain,
             CAST(o.condition_concept_id AS bigint) AS target_concept_id
      FROM [{target_schema}].[condition_occurrence] o
      JOIN source_concepts s
        ON s.target_domain = 'Condition'
       AND s.target_concept_id = o.condition_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.observation_date AS date), 'Observation',
             CAST(o.observation_concept_id AS bigint)
      FROM [{target_schema}].[observation] o
      JOIN source_concepts s
        ON s.target_domain = 'Observation'
       AND s.target_concept_id = o.observation_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.procedure_date AS date), 'Procedure',
             CAST(o.procedure_concept_id AS bigint)
      FROM [{target_schema}].[procedure_occurrence] o
      JOIN source_concepts s
        ON s.target_domain = 'Procedure'
       AND s.target_concept_id = o.procedure_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.measurement_date AS date), 'Measurement',
             CAST(o.measurement_concept_id AS bigint)
      FROM [{target_schema}].[measurement] o
      JOIN source_concepts s
        ON s.target_domain = 'Measurement'
       AND s.target_concept_id = o.measurement_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.drug_exposure_start_date AS date), 'Drug',
             CAST(o.drug_concept_id AS bigint)
      FROM [{target_schema}].[drug_exposure] o
      JOIN source_concepts s
        ON s.target_domain = 'Drug'
       AND s.target_concept_id = o.drug_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.device_exposure_start_date AS date), 'Device',
             CAST(o.device_concept_id AS bigint)
      FROM [{target_schema}].[device_exposure] o
      JOIN source_concepts s
        ON s.target_domain = 'Device'
       AND s.target_concept_id = o.device_concept_id

      UNION ALL
      SELECT o.person_id, CAST(o.specimen_date AS date), 'Specimen',
             CAST(o.specimen_concept_id AS bigint)
      FROM [{target_schema}].[specimen] o
      JOIN source_concepts s
        ON s.target_domain = 'Specimen'
       AND s.target_concept_id = o.specimen_concept_id
    )
    """


def run(config_path: str, output_dir: str | None = None) -> dict[str, object]:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = _schema(sql_cfg.get("source_schema", "dbo"))
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"))

    study_path = Path(STUDY_DEFINITION)
    if not study_path.exists():
        raise RuntimeError(f"Missing locked study definition: {study_path}")
    study = json.loads(study_path.read_text(encoding="utf-8"))
    if study.get("study_definition") != "stage-b-v1":
        raise RuntimeError("Unexpected Stage B study definition")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage B study definition is not anchored to frozen ETL SHA")

    out = (
        Path(output_dir)
        if output_dir
        else config.audit_dir.parent
        / "publication_analysis"
        / "stage_b_patient_concordance"
        / "condition"
    )
    out.mkdir(parents=True, exist_ok=True)

    required = (
        (source_schema, "PCORnet_DIAGNOSIS"),
        (source_schema, "PCORnet_CONDITION"),
        (target_schema, "person"),
        (target_schema, ROUTE_TABLE),
        (target_schema, "condition_occurrence"),
        (target_schema, "observation"),
        (target_schema, "procedure_occurrence"),
        (target_schema, "measurement"),
        (target_schema, "drug_exposure"),
        (target_schema, "device_exposure"),
        (target_schema, "specimen"),
    )

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            ctes = _semantic_ctes(source_schema, target_schema)

            source_core_route_rows = _scalar(con, ctes + " SELECT COUNT_BIG(*) FROM source_semantic")
            source_mapped_route_rows = _scalar(con, ctes + " SELECT COUNT_BIG(*) FROM source_mapped")
            unresolved_fallback_rows = _scalar(
                con,
                ctes + " SELECT COUNT_BIG(*) FROM source_semantic WHERE target_concept_id = 0",
            )
            unresolved_fallback_patients = _scalar(
                con,
                ctes + " SELECT COUNT_BIG(DISTINCT person_id) FROM source_semantic WHERE target_concept_id = 0",
            )
            target_semantic_rows = _scalar(con, ctes + " SELECT COUNT_BIG(*) FROM target_semantic")

            source_event_row = con.execute(text(ctes + """
                SELECT
                  COUNT_BIG(*) AS source_events,
                  SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END) AS multi_route_events
                FROM (
                  SELECT source_domain, source_record_id, COUNT_BIG(*) AS n
                  FROM source_semantic
                  GROUP BY source_domain, source_record_id
                ) q
            """)).one()
            source_events, multi_route_source_events = map(int, source_event_row)

            patient_row = con.execute(text(ctes + """
                , s AS (SELECT DISTINCT person_id FROM source_mapped),
                  t AS (SELECT DISTINCT person_id FROM target_semantic),
                  a AS (SELECT person_id FROM s UNION SELECT person_id FROM t)
                SELECT
                  SUM(CASE WHEN s.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END)
                FROM a
                LEFT JOIN s ON s.person_id=a.person_id
                LEFT JOIN t ON t.person_id=a.person_id
            """)).one()
            source_patients, target_patients, intersection, source_only, target_only = map(int, patient_row)
            patient_union = intersection + source_only + target_only

            signature_row = con.execute(text(ctes + """
                , s AS (
                  SELECT person_id, event_date, target_domain, target_concept_id, COUNT_BIG(*) AS n
                  FROM source_mapped
                  GROUP BY person_id, event_date, target_domain, target_concept_id
                ),
                t AS (
                  SELECT person_id, event_date, target_domain, target_concept_id, COUNT_BIG(*) AS n
                  FROM target_semantic
                  GROUP BY person_id, event_date, target_domain, target_concept_id
                ),
                k AS (
                  SELECT person_id, event_date, target_domain, target_concept_id FROM s
                  UNION
                  SELECT person_id, event_date, target_domain, target_concept_id FROM t
                )
                SELECT
                  SUM(CASE WHEN COALESCE(s.n,0) < COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),
                  SUM(CASE WHEN COALESCE(s.n,0) > COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),
                  SUM(CASE WHEN COALESCE(t.n,0) > COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END),
                  SUM(CASE WHEN s.n IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN t.n IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN s.n IS NOT NULL AND t.n IS NOT NULL THEN 1 ELSE 0 END)
                FROM k
                LEFT JOIN s ON s.person_id=k.person_id AND s.event_date=k.event_date
                           AND s.target_domain=k.target_domain AND s.target_concept_id=k.target_concept_id
                LEFT JOIN t ON t.person_id=k.person_id AND t.event_date=k.event_date
                           AND t.target_domain=k.target_domain AND t.target_concept_id=k.target_concept_id
            """)).one()
            (
                exact_signature_matched_events,
                source_unmatched_signature_events,
                target_unmatched_signature_events,
                source_signature_keys,
                target_signature_keys,
                shared_signature_keys,
            ) = map(int, signature_row)

            domain_rows = []
            for domain in EVENT_DOMAINS:
                row = con.execute(text(ctes + """
                    , s AS (
                      SELECT person_id, event_date, target_concept_id, COUNT_BIG(*) AS n
                      FROM source_mapped WHERE target_domain=:domain
                      GROUP BY person_id, event_date, target_concept_id
                    ),
                    t AS (
                      SELECT person_id, event_date, target_concept_id, COUNT_BIG(*) AS n
                      FROM target_semantic WHERE target_domain=:domain
                      GROUP BY person_id, event_date, target_concept_id
                    ),
                    k AS (
                      SELECT person_id, event_date, target_concept_id FROM s
                      UNION SELECT person_id, event_date, target_concept_id FROM t
                    )
                    SELECT
                      COALESCE((SELECT SUM(n) FROM s),0),
                      COALESCE((SELECT SUM(n) FROM t),0),
                      COALESCE((SELECT COUNT_BIG(DISTINCT person_id) FROM s),0),
                      COALESCE((SELECT COUNT_BIG(DISTINCT person_id) FROM t),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0) < COALESCE(t.n,0) THEN COALESCE(s.n,0) ELSE COALESCE(t.n,0) END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(s.n,0) > COALESCE(t.n,0) THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN COALESCE(t.n,0) > COALESCE(s.n,0) THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END),0)
                    FROM k
                    LEFT JOIN s ON s.person_id=k.person_id AND s.event_date=k.event_date AND s.target_concept_id=k.target_concept_id
                    LEFT JOIN t ON t.person_id=k.person_id AND t.event_date=k.event_date AND t.target_concept_id=k.target_concept_id
                """), {"domain": domain}).one()
                source_rows, target_rows, sp, tp, matched, su, tu = map(int, row)
                domain_rows.append({
                    "target_domain": domain,
                    "source_mapped_route_rows": source_rows,
                    "target_native_rows_in_source_concept_space": target_rows,
                    "source_patients": sp,
                    "target_patients": tp,
                    "exact_signature_matched_events": matched,
                    "source_unmatched_events": su,
                    "target_unmatched_events": tu,
                    "source_match_percent": _pct(matched, source_rows),
                    "target_match_percent": _pct(matched, target_rows),
                })
    finally:
        engine.dispose()

    summary = {
        "status": "stage_b_condition_concordance_complete",
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
        "semantic_reference": (
            "Frozen canonical Condition route ledger supplies the prespecified source-side Standard concept/domain semantics. "
            "Native OMOP tables are queried independently for those concepts; no target lineage/xwalk is used in primary metrics."
        ),
        "primary_comparison": {
            "source_eligible_events_represented_in_route_ledger": source_events,
            "source_core_route_rows": source_core_route_rows,
            "source_mapped_route_rows": source_mapped_route_rows,
            "source_unresolved_fallback_rows": unresolved_fallback_rows,
            "source_unresolved_fallback_patients": unresolved_fallback_patients,
            "source_multi_route_events": multi_route_source_events,
            "target_native_rows_in_source_concept_space": target_semantic_rows,
            "source_mapped_patients": source_patients,
            "target_mapped_patients": target_patients,
            "intersection_patients": intersection,
            "source_only_patients": source_only,
            "target_only_patients": target_only,
            "union_patients": patient_union,
            "patient_jaccard": _jaccard(intersection, patient_union),
            "patient_positive_agreement_percent": _pct(2 * intersection, source_patients + target_patients),
            "exact_person_date_domain_concept_matched_events": exact_signature_matched_events,
            "source_unmatched_signature_events": source_unmatched_signature_events,
            "target_unmatched_signature_events": target_unmatched_signature_events,
            "source_exact_signature_match_percent": _pct(exact_signature_matched_events, source_mapped_route_rows),
            "target_exact_signature_match_percent": _pct(exact_signature_matched_events, target_semantic_rows),
            "source_signature_keys": source_signature_keys,
            "target_signature_keys": target_signature_keys,
            "shared_signature_keys": shared_signature_keys,
        },
        "domain_summary": domain_rows,
        "interpretation_rules": [
            "Concept-0 fallback routes are represented-but-unresolved and excluded from mapped semantic concordance denominators.",
            "One-to-many Standard routes are valid semantic expansion and are not collapsed to force row-count equality.",
            "Cross-domain routes are compared in their OMOP target domains rather than forced into Condition Occurrence.",
            "Exact event matching is a multiset comparison on person, calendar date, OMOP domain, and Standard concept.",
            "Target lineage/xwalk tables are not used in the primary semantic comparison; they may be used later for discordance attribution.",
        ],
    }

    (out / "stage_b_condition_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (out / "condition_domain_concordance.csv").open("w", newline="", encoding="utf-8") as f:
        fields = list(domain_rows[0].keys()) if domain_rows else ["target_domain"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(domain_rows)

    p = summary["primary_comparison"]
    md_lines = [
        "# Stage B Wave 1: Condition semantic concordance",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "The frozen canonical Condition route ledger is the prespecified source-side vocabulary/domain semantic reference. Native OMOP event tables are queried independently for those Standard concepts; target lineage/xwalks are not used in primary metrics.",
        "",
        "## Primary results",
        "",
        f"- Eligible source events represented in the canonical ledger: {source_events:,}",
        f"- Core semantic route rows: {source_core_route_rows:,}",
        f"- Mapped nonzero Standard route rows: {source_mapped_route_rows:,}",
        f"- Unresolved concept-0 fallback rows: {unresolved_fallback_rows:,}",
        f"- Source events with multiple core routes: {multi_route_source_events:,}",
        f"- Source mapped patients: {source_patients:,}",
        f"- Target mapped patients: {target_patients:,}",
        f"- Shared mapped patients: {intersection:,}",
        f"- Patient Jaccard: {p['patient_jaccard']}",
        f"- Exact person/date/domain/concept matched events: {exact_signature_matched_events:,}",
        f"- Source unmatched semantic events: {source_unmatched_signature_events:,}",
        f"- Target unmatched semantic events: {target_unmatched_signature_events:,}",
        "",
        "## Domain summary",
        "",
        "| Domain | Source mapped rows | Native OMOP rows | Exact matched | Source unmatched | Target unmatched |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in domain_rows:
        md_lines.append(
            f"| {r['target_domain']} | {r['source_mapped_route_rows']:,} | {r['target_native_rows_in_source_concept_space']:,} | "
            f"{r['exact_signature_matched_events']:,} | {r['source_unmatched_events']:,} | {r['target_unmatched_events']:,} |"
        )
    (out / "stage_b_condition_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print("status: stage_b_condition_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_events: {source_events}")
    print(f"source_core_route_rows: {source_core_route_rows}")
    print(f"source_mapped_route_rows: {source_mapped_route_rows}")
    print(f"unresolved_fallback_rows: {unresolved_fallback_rows}")
    print(f"multi_route_source_events: {multi_route_source_events}")
    print(f"source_mapped_patients: {source_patients}")
    print(f"target_mapped_patients: {target_patients}")
    print(f"intersection_patients: {intersection}")
    print(f"source_only_patients: {source_only}")
    print(f"target_only_patients: {target_only}")
    print(f"patient_jaccard: {summary['primary_comparison']['patient_jaccard']}")
    print(f"exact_signature_matched_events: {exact_signature_matched_events}")
    print(f"source_unmatched_signature_events: {source_unmatched_signature_events}")
    print(f"target_unmatched_signature_events: {target_unmatched_signature_events}")
    print(f"output_dir: {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Stage B Wave 1 Condition semantic concordance")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run(args.config, args.output_dir)


if __name__ == "__main__":
    main()
