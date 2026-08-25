from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_obs_clin_route"


def materialize_obs_clin_routes(config_path: str, replace: bool = False) -> int:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "obs_clin_routes.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            for schema, table in (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            ):
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Required table [{schema}].[{table}] does not exist")

            exists = table_exists(con, target_schema, ROUTE_TABLE)
            if exists and not replace:
                raise RuntimeError(
                    f"[{target_schema}].[{ROUTE_TABLE}] already exists; use --replace to rebuild"
                )
            if exists:
                con.exec_driver_sql(f"DROP TABLE [{target_schema}].[{ROUTE_TABLE}]")
                con.commit()

            con.exec_driver_sql(f"""
                CREATE TABLE [{target_schema}].[{ROUTE_TABLE}] (
                    route_id bigint NOT NULL,
                    source_obsclin_id nvarchar(255) NOT NULL,
                    patid nvarchar(255) NOT NULL,
                    encounterid nvarchar(255) NULL,
                    obsclin_start_date date NOT NULL,
                    obsclin_start_time float NULL,
                    obsclin_stop_date date NULL,
                    obsclin_stop_time float NULL,
                    obsclin_type nvarchar(20) NOT NULL,
                    obsclin_code nvarchar(255) NOT NULL,
                    source_concept_id bigint NOT NULL,
                    target_domain varchar(50) NOT NULL,
                    target_concept_id bigint NOT NULL,
                    route_status varchar(64) NOT NULL,
                    CONSTRAINT PK_{ROUTE_TABLE} PRIMARY KEY (route_id),
                    CONSTRAINT UQ_{ROUTE_TABLE}_source UNIQUE (source_obsclin_id)
                )
            """)
            con.commit()

            con.exec_driver_sql(f"""
                WITH native AS (
                    SELECT
                        LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID))) AS source_obsclin_id,
                        LTRIM(RTRIM(CONVERT(nvarchar(255), o.PATID))) AS patid,
                        NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), o.ENCOUNTERID))), '') AS encounterid,
                        CAST(o.OBSCLIN_START_DATE AS date) AS obsclin_start_date,
                        TRY_CONVERT(float, o.OBSCLIN_START_TIME) AS obsclin_start_time,
                        CAST(o.OBSCLIN_STOP_DATE AS date) AS obsclin_stop_date,
                        TRY_CONVERT(float, o.OBSCLIN_STOP_TIME) AS obsclin_stop_time,
                        UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), o.OBSCLIN_TYPE)))) AS obsclin_type,
                        LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLIN_CODE))) AS obsclin_code,
                        CASE
                            WHEN UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), o.OBSCLIN_TYPE)))) = 'LC' THEN 'LOINC'
                            WHEN UPPER(LTRIM(RTRIM(CONVERT(nvarchar(20), o.OBSCLIN_TYPE)))) = 'SM' THEN 'SNOMED'
                            ELSE NULL
                        END AS vocabulary_id
                    FROM [{source_schema}].[PCORnet_OBS_CLIN] o
                    WHERE o.OBSCLINID IS NOT NULL
                      AND LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLINID))) <> ''
                      AND o.PATID IS NOT NULL
                      AND LTRIM(RTRIM(CONVERT(nvarchar(255), o.PATID))) <> ''
                      AND o.OBSCLIN_START_DATE IS NOT NULL
                      AND o.OBSCLIN_CODE IS NOT NULL
                      AND LTRIM(RTRIM(CONVERT(nvarchar(255), o.OBSCLIN_CODE))) <> ''
                ),
                source_candidates AS (
                    SELECT
                        n.*,
                        c.concept_id AS source_concept_id,
                        c.domain_id AS source_domain,
                        c.standard_concept,
                        c.invalid_reason
                    FROM native n
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.vocabulary_id = n.vocabulary_id
                     AND c.concept_code = n.obsclin_code
                ),
                candidate_counts AS (
                    SELECT source_obsclin_id, COUNT(DISTINCT source_concept_id) AS n_source_candidates
                    FROM source_candidates
                    GROUP BY source_obsclin_id
                ),
                unique_source AS (
                    SELECT sc.*
                    FROM source_candidates sc
                    JOIN candidate_counts cc
                      ON cc.source_obsclin_id = sc.source_obsclin_id
                    WHERE cc.n_source_candidates = 1
                      AND sc.source_concept_id IS NOT NULL
                ),
                mapped AS (
                    SELECT DISTINCT
                        us.source_obsclin_id,
                        t.concept_id AS target_concept_id,
                        t.domain_id AS target_domain
                    FROM unique_source us
                    JOIN [{target_schema}].[concept_relationship] cr
                      ON cr.concept_id_1 = us.source_concept_id
                     AND cr.relationship_id = 'Maps to'
                     AND (cr.invalid_reason IS NULL OR cr.invalid_reason = '')
                    JOIN [{target_schema}].[concept] t
                      ON t.concept_id = cr.concept_id_2
                     AND t.standard_concept = 'S'
                     AND t.invalid_reason IS NULL
                    WHERE NOT (
                        us.invalid_reason IS NULL
                        AND COALESCE(us.standard_concept, '') = 'S'
                    )
                ),
                mapped_counts AS (
                    SELECT source_obsclin_id, COUNT_BIG(*) AS n_targets
                    FROM mapped
                    GROUP BY source_obsclin_id
                ),
                resolved AS (
                    SELECT
                        sc.source_obsclin_id,
                        sc.patid,
                        sc.encounterid,
                        sc.obsclin_start_date,
                        sc.obsclin_start_time,
                        sc.obsclin_stop_date,
                        sc.obsclin_stop_time,
                        sc.obsclin_type,
                        sc.obsclin_code,
                        COALESCE(us.source_concept_id, 0) AS source_concept_id,
                        CASE
                            WHEN cc.n_source_candidates > 1 THEN 'Observation'
                            WHEN us.invalid_reason IS NULL AND us.standard_concept = 'S'
                                THEN us.source_domain
                            WHEN mc.n_targets = 1 THEN m.target_domain
                            ELSE COALESCE(us.source_domain, 'Observation')
                        END AS target_domain,
                        CASE
                            WHEN cc.n_source_candidates > 1 THEN 0
                            WHEN us.invalid_reason IS NULL AND us.standard_concept = 'S'
                                THEN us.source_concept_id
                            WHEN mc.n_targets = 1 THEN m.target_concept_id
                            ELSE 0
                        END AS target_concept_id,
                        CASE
                            WHEN cc.n_source_candidates > 1 THEN 'ambiguous_source_concept'
                            WHEN us.invalid_reason IS NULL AND us.standard_concept = 'S'
                                THEN 'direct_standard'
                            WHEN mc.n_targets = 1 THEN 'maps_to_standard'
                            WHEN mc.n_targets > 1 THEN 'ambiguous_maps_to'
                            WHEN us.source_concept_id IS NULL THEN 'source_concept_not_found'
                            ELSE 'no_active_standard_target'
                        END AS route_status
                    FROM source_candidates sc
                    JOIN candidate_counts cc
                      ON cc.source_obsclin_id = sc.source_obsclin_id
                    LEFT JOIN unique_source us
                      ON us.source_obsclin_id = sc.source_obsclin_id
                    LEFT JOIN mapped_counts mc
                      ON mc.source_obsclin_id = sc.source_obsclin_id
                    LEFT JOIN mapped m
                      ON m.source_obsclin_id = sc.source_obsclin_id
                     AND mc.n_targets = 1
                )
                INSERT INTO [{target_schema}].[{ROUTE_TABLE}] (
                    route_id, source_obsclin_id, patid, encounterid,
                    obsclin_start_date, obsclin_start_time,
                    obsclin_stop_date, obsclin_stop_time,
                    obsclin_type, obsclin_code, source_concept_id,
                    target_domain, target_concept_id, route_status
                )
                SELECT
                    ROW_NUMBER() OVER (ORDER BY source_obsclin_id),
                    source_obsclin_id, patid, encounterid,
                    obsclin_start_date, obsclin_start_time,
                    obsclin_stop_date, obsclin_stop_time,
                    obsclin_type, obsclin_code, source_concept_id,
                    target_domain, target_concept_id, route_status
                FROM resolved
            """)
            con.commit()

            source_rows = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_OBS_CLIN]
                WHERE OBSCLINID IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLINID))) <> ''
                  AND PATID IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
                  AND OBSCLIN_START_DATE IS NOT NULL
                  AND OBSCLIN_CODE IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(255), OBSCLIN_CODE))) <> ''
            """)).scalar_one())
            route_rows = int(con.execute(text(
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{ROUTE_TABLE}]"
            )).scalar_one())
            if route_rows != source_rows:
                raise RuntimeError(
                    f"OBS_CLIN route reconciliation failed: source={source_rows:,}, routes={route_rows:,}"
                )

            invalid_nonzero = int(con.execute(text(f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[{ROUTE_TABLE}] r
                LEFT JOIN [{target_schema}].[concept] c
                  ON c.concept_id = r.target_concept_id
                WHERE r.target_concept_id <> 0
                  AND (
                       c.concept_id IS NULL
                    OR c.standard_concept <> 'S'
                    OR c.invalid_reason IS NOT NULL
                    OR c.domain_id <> r.target_domain
                  )
            """)).scalar_one())
            if invalid_nonzero:
                raise RuntimeError(
                    f"OBS_CLIN route ledger contains {invalid_nonzero:,} invalid nonzero target concepts"
                )

            domain_summary = [
                {
                    "target_domain": r[0],
                    "rows": int(r[1]),
                    "concept_zero_rows": int(r[2]),
                }
                for r in con.execute(text(f"""
                    SELECT target_domain, COUNT_BIG(*),
                           SUM(CASE WHEN target_concept_id = 0 THEN 1 ELSE 0 END)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    GROUP BY target_domain
                    ORDER BY COUNT_BIG(*) DESC
                """)).fetchall()
            ]
            route_summary = [
                {"route_status": r[0], "rows": int(r[1])}
                for r in con.execute(text(f"""
                    SELECT route_status, COUNT_BIG(*)
                    FROM [{target_schema}].[{ROUTE_TABLE}]
                    GROUP BY route_status
                    ORDER BY COUNT_BIG(*) DESC
                """)).fetchall()
            ]
    finally:
        engine.dispose()

    payload = {
        "stage": "obs_clin_routes",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "route_table": f"{target_schema}.{ROUTE_TABLE}",
        "source_rows": source_rows,
        "route_rows": route_rows,
        "domain_summary": domain_summary,
        "route_summary": route_summary,
        "policy": (
            "Use declared PCORnet LC=LOINC and SM=SNOMED vocabularies only. "
            "Require unique exact source-concept resolution; do not arbitrarily choose among source candidates. "
            "A unique active Standard source concept is retained directly; a unique active Maps to target is used. "
            "Ambiguous Maps to and unsupported/local OT semantics remain concept 0 rather than using site-specific overrides."
        ),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"OBS_CLIN eligible source rows: {source_rows:,}")
    print(f"Route rows: {route_rows:,}")
    for row in domain_summary:
        print(f"{row['target_domain']}: {row['rows']:,} (concept 0: {row['concept_zero_rows']:,})")
    print(f"Audit: {audit_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize audited OBS_CLIN routing ledger.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    return materialize_obs_clin_routes(args.config, replace=args.replace)


if __name__ == "__main__":
    raise SystemExit(main())
