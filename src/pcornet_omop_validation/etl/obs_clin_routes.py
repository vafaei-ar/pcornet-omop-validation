from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import load_etl_config
from .database import make_engine, table_exists


ROUTE_TABLE = "etl_obs_clin_route"

LOCAL_OVERRIDES = {
    "FEV1-FVC-PRE": ("Measurement", 4233038),
    "FEV1-FVC-PRE-PRED": ("Measurement", 3024594),
    "FEV1-FVC-POST-PRED": ("Measurement", 0),
    "DLCO-POST-PRED": ("Measurement", 0),
}


def materialize_obs_clin_routes(
    config_path: str,
    replace: bool = False,
) -> int:
    config = load_etl_config(config_path)
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "obs_clin_routes.json"

    engine = make_engine(config)

    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (target_schema, "concept"),
                (target_schema, "concept_relationship"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            exists = table_exists(con, source_schema, ROUTE_TABLE)
            if exists and not replace:
                raise RuntimeError(
                    f"[{source_schema}].[{ROUTE_TABLE}] already exists; "
                    "use --replace to rebuild"
                )
            if exists:
                con.exec_driver_sql(
                    f"DROP TABLE [{source_schema}].[{ROUTE_TABLE}]"
                )
                con.commit()

            # Validate explicit local standard targets.
            expected = {
                4233038: ("Measurement", "SNOMED", "407602006"),
                3024594: ("Measurement", "LOINC", "19925-7"),
            }
            rows = con.execute(
                text(
                    f"""
                    SELECT concept_id, domain_id, vocabulary_id,
                           concept_code, standard_concept, invalid_reason
                    FROM [{target_schema}].[concept]
                    WHERE concept_id IN (4233038, 3024594)
                    """
                )
            ).fetchall()

            observed = {int(r[0]): r for r in rows}
            for cid, (domain, vocab, code) in expected.items():
                r = observed.get(cid)
                if (
                    r is None
                    or r[1] != domain
                    or r[2] != vocab
                    or r[3] != code
                    or r[4] != "S"
                    or r[5] is not None
                ):
                    raise RuntimeError(
                        f"Validated local target {cid} no longer "
                        "matches expected vocabulary semantics"
                    )

            con.exec_driver_sql(
                f"""
                CREATE TABLE [{source_schema}].[{ROUTE_TABLE}] (
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
                    CONSTRAINT UQ_{ROUTE_TABLE}_source
                        UNIQUE (source_obsclin_id)
                )
                """
            )
            con.commit()

            # LC=LOINC, SM=SNOMED. OT codes use the prespecified local
            # pulmonary-function overrides.
            con.exec_driver_sql(
                f"""
                WITH native AS (
                    SELECT
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), o.OBSCLINID
                        ))) AS source_obsclin_id,
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), o.PATID
                        ))) AS patid,
                        NULLIF(LTRIM(RTRIM(CONVERT(
                            nvarchar(255), o.ENCOUNTERID
                        ))), '') AS encounterid,
                        CAST(o.OBSCLIN_START_DATE AS date)
                            AS obsclin_start_date,
                        TRY_CONVERT(float, o.OBSCLIN_START_TIME)
                            AS obsclin_start_time,
                        CAST(o.OBSCLIN_STOP_DATE AS date)
                            AS obsclin_stop_date,
                        TRY_CONVERT(float, o.OBSCLIN_STOP_TIME)
                            AS obsclin_stop_time,
                        UPPER(LTRIM(RTRIM(CONVERT(
                            nvarchar(20), o.OBSCLIN_TYPE
                        )))) AS obsclin_type,
                        LTRIM(RTRIM(CONVERT(
                            nvarchar(255), o.OBSCLIN_CODE
                        ))) AS obsclin_code,
                        CASE
                            WHEN o.OBSCLIN_TYPE = 'LC' THEN 'LOINC'
                            WHEN o.OBSCLIN_TYPE = 'SM' THEN 'SNOMED'
                            ELSE NULL
                        END AS vocabulary_id
                    FROM [{source_schema}].[PCORnet_OBS_CLIN] o
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
                mapped AS (
                    SELECT DISTINCT
                        sc.source_obsclin_id,
                        t.concept_id AS target_concept_id,
                        t.domain_id AS target_domain
                    FROM source_candidates sc
                    JOIN [{target_schema}].[concept_relationship] cr
                      ON cr.concept_id_1 = sc.source_concept_id
                     AND cr.relationship_id = 'Maps to'
                     AND (
                         cr.invalid_reason IS NULL
                         OR cr.invalid_reason = ''
                     )
                    JOIN [{target_schema}].[concept] t
                      ON t.concept_id = cr.concept_id_2
                     AND t.standard_concept = 'S'
                     AND t.invalid_reason IS NULL
                    WHERE NOT (
                        sc.invalid_reason IS NULL
                        AND COALESCE(sc.standard_concept, '') = 'S'
                    )
                ),
                mapped_counts AS (
                    SELECT
                        source_obsclin_id,
                        COUNT_BIG(*) AS n_targets
                    FROM mapped
                    GROUP BY source_obsclin_id
                ),
                resolved AS (
                    SELECT
                        sc.*,
                        CASE
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'FEV1-FVC-PRE'
                                THEN 'Measurement'
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'FEV1-FVC-PRE-PRED'
                                THEN 'Measurement'
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'FEV1-FVC-POST-PRED'
                                THEN 'Measurement'
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'DLCO-POST-PRED'
                                THEN 'Measurement'
                            WHEN sc.invalid_reason IS NULL
                             AND sc.standard_concept = 'S'
                                THEN sc.source_domain
                            WHEN mc.n_targets = 1
                                THEN m.target_domain
                            ELSE COALESCE(
                                sc.source_domain, 'Observation'
                            )
                        END AS target_domain,
                        CASE
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'FEV1-FVC-PRE'
                                THEN 4233038
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code = 'FEV1-FVC-PRE-PRED'
                                THEN 3024594
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code IN (
                                'FEV1-FVC-POST-PRED',
                                'DLCO-POST-PRED'
                             )
                                THEN 0
                            WHEN sc.invalid_reason IS NULL
                             AND sc.standard_concept = 'S'
                                THEN sc.source_concept_id
                            WHEN mc.n_targets = 1
                                THEN m.target_concept_id
                            ELSE 0
                        END AS target_concept_id,
                        CASE
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code IN (
                                'FEV1-FVC-PRE',
                                'FEV1-FVC-PRE-PRED'
                             )
                                THEN 'validated_local_override'
                            WHEN sc.obsclin_type = 'OT'
                             AND sc.obsclin_code IN (
                                'FEV1-FVC-POST-PRED',
                                'DLCO-POST-PRED'
                             )
                                THEN 'local_measurement_unresolved'
                            WHEN sc.invalid_reason IS NULL
                             AND sc.standard_concept = 'S'
                                THEN 'direct_standard'
                            WHEN mc.n_targets = 1
                                THEN 'maps_to_standard'
                            WHEN mc.n_targets > 1
                                THEN 'ambiguous_maps_to'
                            WHEN sc.source_concept_id IS NULL
                                THEN 'source_concept_not_found'
                            ELSE 'no_active_standard_target'
                        END AS route_status
                    FROM source_candidates sc
                    LEFT JOIN mapped_counts mc
                      ON mc.source_obsclin_id = sc.source_obsclin_id
                    LEFT JOIN mapped m
                      ON m.source_obsclin_id = sc.source_obsclin_id
                     AND mc.n_targets = 1
                )
                INSERT INTO [{source_schema}].[{ROUTE_TABLE}] (
                    route_id,
                    source_obsclin_id,
                    patid,
                    encounterid,
                    obsclin_start_date,
                    obsclin_start_time,
                    obsclin_stop_date,
                    obsclin_stop_time,
                    obsclin_type,
                    obsclin_code,
                    source_concept_id,
                    target_domain,
                    target_concept_id,
                    route_status
                )
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY source_obsclin_id
                    ),
                    source_obsclin_id,
                    patid,
                    encounterid,
                    obsclin_start_date,
                    obsclin_start_time,
                    obsclin_stop_date,
                    obsclin_stop_time,
                    obsclin_type,
                    obsclin_code,
                    COALESCE(source_concept_id, 0),
                    target_domain,
                    target_concept_id,
                    route_status
                FROM resolved
                """
            )
            con.commit()

            source_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[PCORnet_OBS_CLIN]
                        """
                    )
                ).scalar_one()
            )
            route_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{source_schema}].[{ROUTE_TABLE}]
                        """
                    )
                ).scalar_one()
            )

            if route_rows != source_rows:
                raise RuntimeError(
                    f"OBS_CLIN route reconciliation failed: "
                    f"source={source_rows:,}, routes={route_rows:,}"
                )

            summary = [
                {
                    "target_domain": r[0],
                    "route_status": r[1],
                    "target_concept_id": int(r[2]),
                    "rows": int(r[3]),
                }
                for r in con.execute(
                    text(
                        f"""
                        SELECT
                            target_domain,
                            route_status,
                            target_concept_id,
                            COUNT_BIG(*)
                        FROM [{source_schema}].[{ROUTE_TABLE}]
                        GROUP BY
                            target_domain,
                            route_status,
                            target_concept_id
                        ORDER BY COUNT_BIG(*) DESC
                        """
                    )
                ).fetchall()
            ]

            domain_summary = [
                {
                    "target_domain": r[0],
                    "rows": int(r[1]),
                    "concept_zero_rows": int(r[2]),
                }
                for r in con.execute(
                    text(
                        f"""
                        SELECT
                            target_domain,
                            COUNT_BIG(*),
                            SUM(
                                CASE WHEN target_concept_id = 0
                                     THEN 1 ELSE 0 END
                            )
                        FROM [{source_schema}].[{ROUTE_TABLE}]
                        GROUP BY target_domain
                        ORDER BY COUNT_BIG(*) DESC
                        """
                    )
                ).fetchall()
            ]

    finally:
        engine.dispose()

    payload = {
        "stage": "obs_clin_routes",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_rows": source_rows,
        "route_rows": route_rows,
        "domain_summary": domain_summary,
        "route_summary": summary,
        "local_overrides": {
            k: {
                "target_domain": v[0],
                "target_concept_id": v[1],
            }
            for k, v in LOCAL_OVERRIDES.items()
        },
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"OBS_CLIN source rows: {source_rows:,}")
    print(f"Route rows: {route_rows:,}")
    for row in domain_summary:
        print(
            f"{row['target_domain']}: {row['rows']:,} "
            f"(concept 0: {row['concept_zero_rows']:,})"
        )
    print(f"Audit: {audit_path}")

    return 0
