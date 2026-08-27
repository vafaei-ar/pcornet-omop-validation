from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists
from pcornet_omop_validation.study.stage_a_structural_concordance_v2 import run_stage_a


FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(con, sql: str) -> int:
    return int(con.execute(text(sql)).scalar_one() or 0)


def _rows(con, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in con.execute(text(sql)).fetchall()]


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _get(payload: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _source_eligibility(con, source_schema: str, target_schema: str, audit_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    if table_exists(con, source_schema, "PCORnet_PROCEDURES"):
        total = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_PROCEDURES]")
        eligible = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{source_schema}].[PCORnet_PROCEDURES]
            WHERE PX_DATE IS NOT NULL
              AND PROCEDURESID IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), PROCEDURESID))) <> ''
              AND PATID IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
              AND PX IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), PX))) <> ''
            """,
        )
        out.append({
            "source_family": "PROCEDURES",
            "source_rows": total,
            "eligible_rows": eligible,
            "excluded_rows": total - eligible,
            "eligibility_basis": "PX_DATE, PROCEDURESID, PATID, and PX required by frozen Procedure routing logic",
        })

    if table_exists(con, source_schema, "PCORnet_OBS_CLIN"):
        total = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_OBS_CLIN]")
        eligible = _scalar(
            con,
            f"""
            SELECT COUNT_BIG(*)
            FROM [{source_schema}].[PCORnet_OBS_CLIN]
            WHERE OBSCLINID IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLINID))) <> ''
              AND PATID IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
              AND OBSCLIN_START_DATE IS NOT NULL
              AND OBSCLIN_CODE IS NOT NULL
              AND LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLIN_CODE))) <> ''
            """,
        )
        out.append({
            "source_family": "OBS_CLIN",
            "source_rows": total,
            "eligible_rows": eligible,
            "excluded_rows": total - eligible,
            "eligibility_basis": "OBSCLINID, PATID, OBSCLIN_START_DATE, and OBSCLIN_CODE required by frozen OBS_CLIN routing logic",
        })

    if table_exists(con, target_schema, "etl_condition_event_route_v2"):
        eligible_by_family = {
            str(k): int(v)
            for k, v in _rows(
                con,
                f"""
                SELECT source_domain, COUNT_BIG(DISTINCT source_record_id)
                FROM [{target_schema}].[etl_condition_event_route_v2]
                GROUP BY source_domain
                """,
            )
        }
        for family, table in (("DIAGNOSIS", "PCORnet_DIAGNOSIS"), ("CONDITION", "PCORnet_CONDITION")):
            if table_exists(con, source_schema, table):
                total = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}]")
                eligible = int(eligible_by_family.get(family, 0))
                out.append({
                    "source_family": family,
                    "source_rows": total,
                    "eligible_rows": eligible,
                    "excluded_rows": total - eligible,
                    "eligibility_basis": "canonical eligible event population recorded by frozen Condition route ledger",
                })

    if table_exists(con, target_schema, "etl_drug_event_route"):
        routed = {
            str(k): int(v)
            for k, v in _rows(
                con,
                f"""
                SELECT source_domain, COUNT_BIG(DISTINCT source_record_id)
                FROM [{target_schema}].[etl_drug_event_route]
                GROUP BY source_domain
                """,
            )
        }
        for family, table in (
            ("PRESCRIBING", "PCORnet_PRESCRIBING"),
            ("DISPENSING", "PCORnet_DISPENSING"),
            ("MED_ADMIN", "PCORnet_MED_ADMIN"),
            ("IMMUNIZATION", "PCORnet_IMMUNIZATION"),
        ):
            if table_exists(con, source_schema, table):
                total = _scalar(con, f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{table}]")
                eligible = int(routed.get(family, 0))
                out.append({
                    "source_family": family,
                    "source_rows": total,
                    "eligible_rows": eligible,
                    "excluded_rows": total - eligible,
                    "eligibility_basis": "distinct source records represented in frozen Drug event-route ledger",
                })

    death_path = audit_dir / "clean_build_phase11_death.json"
    if death_path.is_file():
        death = _load_json(death_path)
        d = _get(death, "death_result", default={})
        if isinstance(d, dict) and d.get("source_rows") is not None:
            total = int(d.get("source_rows", 0))
            eligible = int(d.get("eligible_rows", 0))
            out.append({
                "source_family": "DEATH",
                "source_rows": total,
                "eligible_rows": eligible,
                "excluded_rows": total - eligible,
                "eligibility_basis": (
                    "frozen Death audit: missing PATID/date and unlinked-person exclusions; "
                    f"missing_patid={int(d.get('excluded_missing_patid', 0))}, "
                    f"missing_date={int(d.get('excluded_missing_death_date', 0))}, "
                    f"unlinked_person={int(d.get('excluded_unlinked_person', 0))}"
                ),
            })

    return out


def _procedure_summaries(con, schema: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not table_exists(con, schema, "etl_procedure_event_route"):
        raise RuntimeError(f"[{schema}].[etl_procedure_event_route] is required for manuscript summaries")

    disposition = [
        {"disposition": str(d), "route_rows": int(n), "source_events": int(s)}
        for d, n, s in _rows(
            con,
            f"""
            SELECT disposition, COUNT_BIG(*), COUNT_BIG(DISTINCT source_procedure_id)
            FROM [{schema}].[etl_procedure_event_route]
            GROUP BY disposition
            ORDER BY COUNT_BIG(*) DESC, disposition
            """,
        )
    ]
    target_domain = [
        {
            "target_domain": str(domain),
            "route_rows": int(n),
            "source_events": int(s),
            "concept_zero_rows": int(z),
        }
        for domain, n, s, z in _rows(
            con,
            f"""
            SELECT target_domain, COUNT_BIG(*), COUNT_BIG(DISTINCT source_procedure_id),
                   SUM(CASE WHEN target_concept_id=0 THEN 1 ELSE 0 END)
            FROM [{schema}].[etl_procedure_event_route]
            GROUP BY target_domain
            ORDER BY COUNT_BIG(*) DESC, target_domain
            """,
        )
    ]
    mapping = [
        {
            "source_family": "PROCEDURES",
            "mapping_mechanism": str(status),
            "disposition": str(disp),
            "rows": int(n),
        }
        for status, disp, n in _rows(
            con,
            f"""
            SELECT route_status, disposition, COUNT_BIG(*)
            FROM [{schema}].[etl_procedure_event_route]
            GROUP BY route_status, disposition
            ORDER BY COUNT_BIG(*) DESC, route_status, disposition
            """,
        )
    ]
    return disposition, target_domain, mapping


def _condition_summary(con, schema: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not table_exists(con, schema, "etl_condition_event_route_v2"):
        raise RuntimeError(f"[{schema}].[etl_condition_event_route_v2] is required for manuscript summaries")
    detail = [
        {
            "source_family": str(source),
            "target_domain": str(domain),
            "route_status": str(status),
            "is_core_event_route": bool(core),
            "is_fallback": bool(fallback),
            "route_rows": int(n),
            "source_events": int(s),
        }
        for source, domain, status, core, fallback, n, s in _rows(
            con,
            f"""
            SELECT source_domain, target_domain, route_status, is_core_event_route, is_fallback,
                   COUNT_BIG(*), COUNT_BIG(DISTINCT source_record_id)
            FROM [{schema}].[etl_condition_event_route_v2]
            GROUP BY source_domain, target_domain, route_status, is_core_event_route, is_fallback
            ORDER BY COUNT_BIG(*) DESC, source_domain, target_domain, route_status
            """,
        )
    ]
    mapping = [
        {
            "source_family": "DIAGNOSIS/CONDITION",
            "mapping_mechanism": str(status),
            "disposition": "fallback" if bool(fallback) else ("event_route" if bool(core) else "non_event_semantic_component"),
            "rows": int(n),
        }
        for status, core, fallback, n in _rows(
            con,
            f"""
            SELECT route_status, is_core_event_route, is_fallback, COUNT_BIG(*)
            FROM [{schema}].[etl_condition_event_route_v2]
            GROUP BY route_status, is_core_event_route, is_fallback
            ORDER BY COUNT_BIG(*) DESC, route_status
            """,
        )
    ]
    return detail, mapping


def _obsclin_summary(con, schema: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not table_exists(con, schema, "etl_obs_clin_route"):
        raise RuntimeError(f"[{schema}].[etl_obs_clin_route] is required for manuscript summaries")
    detail = [
        {
            "target_domain": str(domain),
            "route_status": str(status),
            "rows": int(n),
            "concept_zero_rows": int(z),
        }
        for domain, status, n, z in _rows(
            con,
            f"""
            SELECT target_domain, route_status, COUNT_BIG(*),
                   SUM(CASE WHEN target_concept_id=0 THEN 1 ELSE 0 END)
            FROM [{schema}].[etl_obs_clin_route]
            GROUP BY target_domain, route_status
            ORDER BY COUNT_BIG(*) DESC, target_domain, route_status
            """,
        )
    ]
    mapping = [
        {
            "source_family": "OBS_CLIN",
            "mapping_mechanism": str(status),
            "disposition": str(domain),
            "rows": int(n),
        }
        for status, domain, n in _rows(
            con,
            f"""
            SELECT route_status, target_domain, COUNT_BIG(*)
            FROM [{schema}].[etl_obs_clin_route]
            GROUP BY route_status, target_domain
            ORDER BY COUNT_BIG(*) DESC, route_status, target_domain
            """,
        )
    ]
    return detail, mapping


def _drug_mapping(con, schema: str) -> list[dict[str, Any]]:
    if not table_exists(con, schema, "etl_drug_event_route"):
        return []
    return [
        {
            "source_family": str(source),
            "mapping_mechanism": str(basis),
            "disposition": str(disposition),
            "rows": int(n),
        }
        for source, basis, disposition, n in _rows(
            con,
            f"""
            SELECT source_domain, mapping_basis, disposition, COUNT_BIG(*)
            FROM [{schema}].[etl_drug_event_route]
            GROUP BY source_domain, mapping_basis, disposition
            ORDER BY COUNT_BIG(*) DESC, source_domain, mapping_basis
            """,
        )
    ]


def run_stage_a_manuscript_tables(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    core = run_stage_a(config_path, output_dir=output_dir)
    if core.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage A core summary is not anchored to the publication ETL freeze")

    config = load_etl_config(config_path)
    source_schema = _schema(config.raw["sqlserver"].get("source_schema", "dbo"), "source_schema")
    target_schema = _schema(config.raw["sqlserver"].get("target_schema", "dbo"), "target_schema")
    audit_dir = Path(config.audit_dir).expanduser().resolve()
    result_root = Path(core["output_dir"]).resolve()

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            source_eligibility = _source_eligibility(con, source_schema, target_schema, audit_dir)
            procedure_disposition, procedure_domain, procedure_mapping = _procedure_summaries(con, target_schema)
            condition_detail, condition_mapping = _condition_summary(con, target_schema)
            obsclin_detail, obsclin_mapping = _obsclin_summary(con, target_schema)
            mapping_mechanism = procedure_mapping + condition_mapping + obsclin_mapping + _drug_mapping(con, target_schema)
    finally:
        engine.dispose()

    _write_csv(
        result_root / "source_eligibility_exclusions.csv",
        source_eligibility,
        ["source_family", "source_rows", "eligible_rows", "excluded_rows", "eligibility_basis"],
    )
    _write_csv(
        result_root / "procedure_route_disposition.csv",
        procedure_disposition,
        ["disposition", "route_rows", "source_events"],
    )
    _write_csv(
        result_root / "procedure_route_target_domain.csv",
        procedure_domain,
        ["target_domain", "route_rows", "source_events", "concept_zero_rows"],
    )
    _write_csv(
        result_root / "condition_route_disposition.csv",
        condition_detail,
        ["source_family", "target_domain", "route_status", "is_core_event_route", "is_fallback", "route_rows", "source_events"],
    )
    _write_csv(
        result_root / "obs_clin_route_domain.csv",
        obsclin_detail,
        ["target_domain", "route_status", "rows", "concept_zero_rows"],
    )
    _write_csv(
        result_root / "mapping_mechanism_summary.csv",
        mapping_mechanism,
        ["source_family", "mapping_mechanism", "disposition", "rows"],
    )

    md = [
        "# Stage A manuscript-oriented tables",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        f"Analysis code SHA: `{core.get('analysis_git_sha')}`",
        "",
        "## Source eligibility and exclusions",
        "",
        "| Source family | Source rows | Eligible/routed rows | Excluded rows |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in source_eligibility:
        md.append(
            f"| {row['source_family']} | {int(row['source_rows']):,} | "
            f"{int(row['eligible_rows']):,} | {int(row['excluded_rows']):,} |"
        )

    md.extend([
        "",
        "## Procedure route disposition",
        "",
        "| Disposition | Route rows | Source events |",
        "| --- | ---: | ---: |",
    ])
    for row in procedure_disposition:
        md.append(f"| {row['disposition']} | {int(row['route_rows']):,} | {int(row['source_events']):,} |")

    md.extend([
        "",
        "## Procedure target domains",
        "",
        "| Target domain | Route rows | Source events | Concept 0 rows |",
        "| --- | ---: | ---: | ---: |",
    ])
    for row in procedure_domain:
        md.append(
            f"| {row['target_domain']} | {int(row['route_rows']):,} | "
            f"{int(row['source_events']):,} | {int(row['concept_zero_rows']):,} |"
        )

    md.extend([
        "",
        "## Interpretation notes",
        "",
        "- These tables are read-only summaries of the frozen source, route ledgers, and audit bundle.",
        "- Counts are outcomes, not ETL acceptance thresholds.",
        "- Route-status labels are retained verbatim from the frozen ETL ledgers so manuscript interpretation remains traceable to implemented vocabulary semantics.",
        "- DIAGNOSIS/CONDITION eligibility is defined by membership in the frozen canonical route population; this table does not invent a more granular exclusion reason when the audit does not encode one.",
        "- Drug source-family eligibility uses distinct source records represented in the frozen drug route ledger. Procedure-derived Drug routes are summarized as routing behavior rather than double-counted as a separate source table.",
        "- The historical comparator database is not queried by this analysis.",
    ])
    manuscript_path = result_root / "stage_a_manuscript_tables.md"
    manuscript_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    return {
        "status": "stage_a_manuscript_tables_complete",
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "analysis_git_sha": core.get("analysis_git_sha"),
        "analysis_worktree_clean": core.get("analysis_worktree_clean"),
        "source_eligibility_rows": len(source_eligibility),
        "procedure_disposition_rows": len(procedure_disposition),
        "procedure_target_domain_rows": len(procedure_domain),
        "condition_route_rows": len(condition_detail),
        "obs_clin_route_rows": len(obsclin_detail),
        "mapping_mechanism_rows": len(mapping_mechanism),
        "output_dir": str(result_root),
        "manuscript_markdown": str(manuscript_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build manuscript-oriented Stage A tables from the frozen audit bundle and route ledgers."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    result = run_stage_a_manuscript_tables(args.config, output_dir=args.output_dir)
    for key in (
        "status",
        "frozen_etl_sha",
        "analysis_git_sha",
        "analysis_worktree_clean",
        "source_eligibility_rows",
        "procedure_disposition_rows",
        "procedure_target_domain_rows",
        "condition_route_rows",
        "obs_clin_route_rows",
        "mapping_mechanism_rows",
        "output_dir",
    ):
        print(f"{key}:", result[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
