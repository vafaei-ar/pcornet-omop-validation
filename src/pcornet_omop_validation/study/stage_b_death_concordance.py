from __future__ import annotations

import argparse
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
        CAST(d.DEATH_DATE AS date) AS death_date
      FROM [{source_schema}].[PCORnet_DEATH] d
      JOIN [{target_schema}].[person] p
        ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
      WHERE d.PATID IS NOT NULL
        AND LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) <> ''
        AND d.DEATH_DATE IS NOT NULL
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

    out = Path(output_dir) if output_dir else (
        config.audit_dir.parent / "publication_analysis" / "stage_b_patient_concordance" / "death"
    )
    out.mkdir(parents=True, exist_ok=True)

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_DEATH"),
                (target_schema, "person"),
                (target_schema, "death"),
                (target_schema, "etl_death_xwalk"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            duplicate_source_patids = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) AS patid
                  FROM [{source_schema}].[PCORnet_DEATH]
                  WHERE PATID IS NOT NULL
                    AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
                  GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), PATID)))
                  HAVING COUNT_BIG(*) > 1
                ) x
            """)
            if duplicate_source_patids:
                raise RuntimeError(
                    f"PCORnet_DEATH has duplicate PATID groups: {duplicate_source_patids:,}"
                )

            duplicate_target_persons = _scalar(con, f"""
                SELECT COUNT_BIG(*) FROM (
                  SELECT person_id
                  FROM [{target_schema}].[death]
                  GROUP BY person_id
                  HAVING COUNT_BIG(*) > 1
                ) x
            """)
            if duplicate_target_persons:
                raise RuntimeError(
                    f"OMOP death has duplicate person_id groups: {duplicate_target_persons:,}"
                )

            src_cte = _source_eligible_cte(source_schema, target_schema)
            source_events = _scalar(con, src_cte + " SELECT COUNT_BIG(*) FROM source_eligible")
            target_events = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death]")

            patient_row = con.execute(text(src_cte + f"""
                , source_patients AS (
                    SELECT DISTINCT person_id FROM source_eligible
                ),
                target_patients AS (
                    SELECT DISTINCT person_id FROM [{target_schema}].[death]
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

            date_row = con.execute(text(src_cte + f"""
                , s AS (
                    SELECT person_id, death_date FROM source_eligible
                ),
                t AS (
                    SELECT person_id, death_date FROM [{target_schema}].[death]
                ),
                all_patients AS (
                    SELECT person_id FROM s UNION SELECT person_id FROM t
                )
                SELECT
                    SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL
                              AND s.death_date = t.death_date THEN 1 ELSE 0 END) AS exact_date,
                    SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NOT NULL
                              AND s.death_date <> t.death_date THEN 1 ELSE 0 END) AS discordant_date,
                    SUM(CASE WHEN s.person_id IS NOT NULL AND t.person_id IS NULL THEN 1 ELSE 0 END) AS source_only,
                    SUM(CASE WHEN s.person_id IS NULL AND t.person_id IS NOT NULL THEN 1 ELSE 0 END) AS target_only
                FROM all_patients a
                LEFT JOIN s ON s.person_id = a.person_id
                LEFT JOIN t ON t.person_id = a.person_id
            """)).one()
            exact_date_matches, discordant_date_pairs, date_source_only, date_target_only = map(int, date_row)

            xwalk_row = con.execute(text(f"""
                SELECT
                    COUNT_BIG(*) AS xwalk_rows,
                    SUM(CASE WHEN d.person_id IS NULL THEN 1 ELSE 0 END) AS missing_target,
                    SUM(CASE WHEN p.person_id IS NULL THEN 1 ELSE 0 END) AS missing_source_person,
                    SUM(CASE WHEN d.person_id IS NOT NULL AND x.source_death_date = d.death_date
                             THEN 1 ELSE 0 END) AS exact_date
                FROM [{target_schema}].[etl_death_xwalk] x
                LEFT JOIN [{target_schema}].[death] d
                  ON d.person_id = x.person_id
                LEFT JOIN [{target_schema}].[person] p
                  ON p.person_id = x.person_id
            """)).one()
            xwalk_rows, xwalk_missing_target, xwalk_missing_person, xwalk_exact_date = map(int, xwalk_row)
    finally:
        engine.dispose()

    summary = {
        "status": "stage_b_death_concordance_complete",
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
            "method": "independent native PCORnet DEATH and OMOP death comparison using the fixed patient bridge; no death xwalk in primary metrics",
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
            "exact_date_matches": exact_date_matches,
            "discordant_date_pairs": discordant_date_pairs,
            "source_only_date_patients": date_source_only,
            "target_only_date_patients": date_target_only,
            "exact_date_match_percent_among_shared_patients": _pct(exact_date_matches, intersection),
        },
        "secondary_lineage_attribution": {
            "method": "frozen death xwalk used only after primary comparison",
            "xwalk_rows": xwalk_rows,
            "missing_target_rows": xwalk_missing_target,
            "missing_person_rows": xwalk_missing_person,
            "exact_date_matches": xwalk_exact_date,
        },
        "interpretation_rules": [
            "Primary death concordance compares patient presence and exact calendar death date without using ETL lineage.",
            "Death type and cause concepts are not part of Stage B semantic concordance because the frozen ETL deliberately retains concept 0 for unsupported provenance/cause semantics.",
            "The death xwalk is secondary evidence for attribution only and must not redefine the primary result.",
        ],
    }

    (out / "stage_b_death_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    p = summary["primary_comparison"]
    (out / "stage_b_death_summary.md").write_text(
        "# Stage B Wave 1: Death concordance\n\n"
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`\n\n"
        "## Primary CDM-native comparison\n\n"
        f"- Source eligible death records: {source_events:,}\n"
        f"- OMOP death rows: {target_events:,}\n"
        f"- Source patients: {source_patients:,}\n"
        f"- OMOP patients: {target_patients:,}\n"
        f"- Shared patients: {intersection:,}\n"
        f"- Source-only patients: {source_only:,}\n"
        f"- OMOP-only patients: {target_only:,}\n"
        f"- Patient Jaccard: {p['patient_jaccard']}\n"
        f"- Exact death-date matches: {exact_date_matches:,}\n"
        f"- Discordant death-date pairs: {discordant_date_pairs:,}\n\n"
        "Primary metrics are computed without the frozen death xwalk; lineage is secondary attribution only.\n",
        encoding="utf-8",
    )

    print("status: stage_b_death_concordance_complete")
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
    print(f"patient_jaccard: {p['patient_jaccard']}")
    print(f"exact_date_matches: {exact_date_matches}")
    print(f"discordant_date_pairs: {discordant_date_pairs}")
    print(f"output_dir: {out}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B Wave 1 death concordance")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    run(args.config, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
