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


def _source_eligible_cte(source_schema: str, target_schema: str) -> str:
    return f"""
    WITH source_eligible AS (
      SELECT
        p.person_id,
        CONVERT(nvarchar(255), e.ENCOUNTERID) AS encounterid,
        CAST(e.ADMIT_DATE AS date) AS event_date,
        UPPER(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(20), e.ENC_TYPE))), '')) AS enc_type
      FROM [{source_schema}].[PCORnet_ENCOUNTER] e
      JOIN [{target_schema}].[person] p
        ON CONVERT(nvarchar(50), e.PATID) = p.person_source_value
      WHERE e.ENCOUNTERID IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), e.ENCOUNTERID))) <> ''
        AND e.PATID IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), e.PATID))) <> ''
        AND e.ADMIT_DATE IS NOT NULL
        AND e.DISCHARGE_DATE IS NOT NULL
        AND CAST(e.DISCHARGE_DATE AS date) >= CAST(e.ADMIT_DATE AS date)
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
        raise RuntimeError("Stage B study definition is not anchored to the frozen ETL SHA")

    out = Path(output_dir) if output_dir else config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "encounter"
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_ENCOUNTER"),
                (target_schema, "person"),
                (target_schema, "visit_occurrence"),
                (target_schema, "etl_visit_occurrence_xwalk"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            src_cte = _source_eligible_cte(source_schema, target_schema)

            source_events = _scalar(con, src_cte + " SELECT COUNT_BIG(*) FROM source_eligible")
            target_events = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence]")

            patient_row = con.execute(text(src_cte + f"""
                , source_patients AS (
                    SELECT DISTINCT person_id FROM source_eligible
                ),
                target_patients AS (
                    SELECT DISTINCT person_id FROM [{target_schema}].[visit_occurrence]
                ),
                all_patients AS (
                    SELECT person_id FROM source_patients
                    UNION
                    SELECT person_id FROM target_patients
                )
                SELECT
                    SUM(CASE WHEN s.person_id IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END)
                FROM all_patients a
                LEFT JOIN source_patients s ON s.person_id = a.person_id
                LEFT JOIN target_patients t ON t.person_id = a.person_id
            """)).one()
            source_patients, target_patients, intersection, source_only, target_only = map(int, patient_row)
            patient_union = intersection + source_only + target_only

            count_row = con.execute(text(src_cte + f"""
                , s AS (
                    SELECT person_id, COUNT_BIG(*) AS n
                    FROM source_eligible GROUP BY person_id
                ),
                t AS (
                    SELECT person_id, COUNT_BIG(*) AS n
                    FROM [{target_schema}].[visit_occurrence]
                    GROUP BY person_id
                ),
                a AS (
                    SELECT person_id FROM s UNION SELECT person_id FROM t
                )
                SELECT
                    COUNT_BIG(*),
                    SUM(CASE WHEN COALESCE(s.n,0) = COALESCE(t.n,0) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN COALESCE(s.n,0) <> COALESCE(t.n,0) THEN 1 ELSE 0 END),
                    SUM(ABS(COALESCE(s.n,0) - COALESCE(t.n,0))),
                    MAX(ABS(COALESCE(s.n,0) - COALESCE(t.n,0)))
                FROM a
                LEFT JOIN s ON s.person_id=a.person_id
                LEFT JOIN t ON t.person_id=a.person_id
            """)).one()
            patients_compared, equal_count_patients, unequal_count_patients, total_abs_count_difference, max_abs_count_difference = map(int, count_row)

            date_row = con.execute(text(src_cte + f"""
                , s AS (
                    SELECT person_id, event_date, COUNT_BIG(*) AS n
                    FROM source_eligible
                    GROUP BY person_id, event_date
                ),
                t AS (
                    SELECT person_id, visit_start_date AS event_date, COUNT_BIG(*) AS n
                    FROM [{target_schema}].[visit_occurrence]
                    GROUP BY person_id, visit_start_date
                ),
                keys AS (
                    SELECT person_id, event_date FROM s
                    UNION
                    SELECT person_id, event_date FROM t
                )
                SELECT
                    SUM(CASE
                        WHEN COALESCE(s.n,0) < COALESCE(t.n,0) THEN COALESCE(s.n,0)
                        ELSE COALESCE(t.n,0)
                    END) AS matched_events,
                    SUM(CASE WHEN COALESCE(s.n,0) > COALESCE(t.n,0)
                             THEN COALESCE(s.n,0)-COALESCE(t.n,0) ELSE 0 END) AS source_unmatched,
                    SUM(CASE WHEN COALESCE(t.n,0) > COALESCE(s.n,0)
                             THEN COALESCE(t.n,0)-COALESCE(s.n,0) ELSE 0 END) AS target_unmatched,
                    SUM(CASE WHEN s.n IS NOT NULL AND t.n IS NOT NULL THEN 1 ELSE 0 END) AS shared_person_date_keys,
                    SUM(CASE WHEN s.n IS NOT NULL THEN 1 ELSE 0 END) AS source_person_date_keys,
                    SUM(CASE WHEN t.n IS NOT NULL THEN 1 ELSE 0 END) AS target_person_date_keys
                FROM keys k
                LEFT JOIN s ON s.person_id=k.person_id AND s.event_date=k.event_date
                LEFT JOIN t ON t.person_id=k.person_id AND t.event_date=k.event_date
            """)).one()
            (
                exact_date_matched_events,
                source_unmatched_date_events,
                target_unmatched_date_events,
                shared_person_date_keys,
                source_person_date_keys,
                target_person_date_keys,
            ) = map(int, date_row)

            xwalk_row = con.execute(text(f"""
                SELECT
                    COUNT_BIG(*) AS xwalk_rows,
                    SUM(CASE WHEN v.visit_occurrence_id IS NULL THEN 1 ELSE 0 END) AS missing_target,
                    SUM(CASE WHEN e.ENCOUNTERID IS NULL THEN 1 ELSE 0 END) AS missing_source,
                    SUM(CASE WHEN e.ENCOUNTERID IS NOT NULL AND v.visit_occurrence_id IS NOT NULL
                              AND CAST(e.ADMIT_DATE AS date) = v.visit_start_date THEN 1 ELSE 0 END) AS exact_start_date,
                    SUM(CASE WHEN e.ENCOUNTERID IS NOT NULL AND v.visit_occurrence_id IS NOT NULL
                              AND p.person_id = v.person_id THEN 1 ELSE 0 END) AS exact_person
                FROM [{target_schema}].[etl_visit_occurrence_xwalk] x
                LEFT JOIN [{source_schema}].[PCORnet_ENCOUNTER] e
                  ON CONVERT(nvarchar(255), e.ENCOUNTERID) = x.encounterid
                LEFT JOIN [{target_schema}].[visit_occurrence] v
                  ON v.visit_occurrence_id = x.visit_occurrence_id
                LEFT JOIN [{target_schema}].[person] p
                  ON CONVERT(nvarchar(50), e.PATID) = p.person_source_value
            """)).one()
            xwalk_rows, xwalk_missing_target, xwalk_missing_source, xwalk_exact_start_date, xwalk_exact_person = map(int, xwalk_row)

            source_type_rows = [
                {"enc_type": row[0] if row[0] is not None else "<NULL>", "rows": int(row[1]), "patients": int(row[2])}
                for row in con.execute(text(src_cte + """
                    SELECT COALESCE(enc_type, '<NULL>'), COUNT_BIG(*), COUNT_BIG(DISTINCT person_id)
                    FROM source_eligible
                    GROUP BY enc_type
                    ORDER BY COUNT_BIG(*) DESC, enc_type
                """)).fetchall()
            ]
            target_type_rows = [
                {
                    "visit_concept_id": int(row[0]),
                    "visit_source_value": row[1] if row[1] is not None else "<NULL>",
                    "rows": int(row[2]),
                    "patients": int(row[3]),
                }
                for row in con.execute(text(f"""
                    SELECT visit_concept_id, COALESCE(visit_source_value, '<NULL>'),
                           COUNT_BIG(*), COUNT_BIG(DISTINCT person_id)
                    FROM [{target_schema}].[visit_occurrence]
                    GROUP BY visit_concept_id, visit_source_value
                    ORDER BY COUNT_BIG(*) DESC, visit_concept_id, visit_source_value
                """)).fetchall()
            ]
    finally:
        engine.dispose()

    summary = {
        "status": "stage_b_encounter_concordance_complete",
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
            "method": "independent CDM-native patient/event aggregates; no xwalk used in primary metrics",
            "source_events": source_events,
            "target_events": target_events,
            "source_patients": source_patients,
            "target_patients": target_patients,
            "intersection_patients": intersection,
            "source_only_patients": source_only,
            "target_only_patients": target_only,
            "union_patients": patient_union,
            "patient_jaccard": _jaccard(intersection, patient_union),
            "patient_positive_agreement_percent": _pct(2 * intersection, source_patients + target_patients),
            "patients_compared_for_event_counts": patients_compared,
            "patients_with_equal_event_count": equal_count_patients,
            "patients_with_unequal_event_count": unequal_count_patients,
            "total_absolute_event_count_difference": total_abs_count_difference,
            "mean_absolute_event_count_difference": None if patients_compared == 0 else total_abs_count_difference / patients_compared,
            "max_absolute_event_count_difference": max_abs_count_difference,
            "exact_date_matched_events": exact_date_matched_events,
            "source_unmatched_date_events": source_unmatched_date_events,
            "target_unmatched_date_events": target_unmatched_date_events,
            "source_exact_date_match_percent": _pct(exact_date_matched_events, source_events),
            "target_exact_date_match_percent": _pct(exact_date_matched_events, target_events),
            "source_person_date_keys": source_person_date_keys,
            "target_person_date_keys": target_person_date_keys,
            "shared_person_date_keys": shared_person_date_keys,
        },
        "secondary_lineage_attribution": {
            "method": "frozen encounter-to-visit xwalk used only after primary metrics",
            "xwalk_rows": xwalk_rows,
            "missing_source_rows": xwalk_missing_source,
            "missing_target_rows": xwalk_missing_target,
            "exact_person_matches": xwalk_exact_person,
            "exact_start_date_matches": xwalk_exact_start_date,
        },
        "interpretation_rules": [
            "Primary patient/event agreement is computed without the ETL xwalk.",
            "Encounter type is reported descriptively; exact source-to-visit concept equality is not a concordance requirement.",
            "Exact date matching uses person plus calendar date with multiset counts, avoiding arbitrary event pairing.",
            "The xwalk is secondary evidence for attribution only and must not redefine the primary result.",
        ],
    }

    summary_path = out / "stage_b_encounter_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    with (out / "source_encounter_type_distribution.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["enc_type", "rows", "patients"])
        w.writeheader(); w.writerows(source_type_rows)
    with (out / "target_visit_type_distribution.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["visit_concept_id", "visit_source_value", "rows", "patients"])
        w.writeheader(); w.writerows(target_type_rows)

    md = out / "stage_b_encounter_summary.md"
    p = summary["primary_comparison"]
    md.write_text(
        "# Stage B Wave 1: Encounter concordance\n\n"
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`\n\n"
        "## Primary CDM-native comparison\n\n"
        f"- Source eligible encounters: {source_events:,}\n"
        f"- OMOP visits: {target_events:,}\n"
        f"- Source patients: {source_patients:,}\n"
        f"- OMOP patients: {target_patients:,}\n"
        f"- Shared patients: {intersection:,}\n"
        f"- PCORnet-only patients: {source_only:,}\n"
        f"- OMOP-only patients: {target_only:,}\n"
        f"- Patient Jaccard: {p['patient_jaccard']:.6f}\n"
        f"- Patients with unequal encounter counts: {unequal_count_patients:,}\n"
        f"- Exact-date matched events: {exact_date_matched_events:,}\n"
        f"- Source unmatched date events: {source_unmatched_date_events:,}\n"
        f"- Target unmatched date events: {target_unmatched_date_events:,}\n\n"
        "## Secondary lineage attribution\n\n"
        f"- Xwalk rows: {xwalk_rows:,}\n"
        f"- Missing source rows: {xwalk_missing_source:,}\n"
        f"- Missing target rows: {xwalk_missing_target:,}\n"
        f"- Exact patient matches: {xwalk_exact_person:,}\n"
        f"- Exact start-date matches: {xwalk_exact_start_date:,}\n",
        encoding="utf-8",
    )

    print("status: stage_b_encounter_concordance_complete")
    print(f"frozen_etl_sha: {FROZEN_ETL_SHA}")
    print(f"analysis_git_sha: {summary['analysis_git_sha']}")
    print(f"analysis_worktree_clean: {summary['analysis_worktree_clean']}")
    print(f"source_events: {source_events}")
    print(f"target_events: {target_events}")
    print(f"source_patients: {source_patients}")
    print(f"target_patients: {target_patients}")
    print(f"intersection_patients: {intersection}")
    print(f"source_only_patients: {source_only}")
    print(f"target_only_patients: {target_only}")
    print(f"patient_jaccard: {summary['primary_comparison']['patient_jaccard']}")
    print(f"unequal_event_count_patients: {unequal_count_patients}")
    print(f"exact_date_matched_events: {exact_date_matched_events}")
    print(f"source_unmatched_date_events: {source_unmatched_date_events}")
    print(f"target_unmatched_date_events: {target_unmatched_date_events}")
    print(f"output_dir: {out}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B Wave 1 encounter/visit patient-level concordance.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    run(args.config, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
