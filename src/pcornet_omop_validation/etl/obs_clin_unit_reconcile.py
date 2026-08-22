from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


def reconcile_obs_clin_units(config: EtlConfig, apply: bool = False) -> dict:
    """Reconcile OBS_CLIN Measurement units to exact UCUM semantics.

    This stage is for an already-materialized validated database after the
    primary OBS_CLIN unit rule was corrected. A fresh ETL run should not need
    this reconciliation.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "obs_clin_unit_reconcile.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_OBS_CLIN"),
                (source_schema, "etl_measurement_xwalk"),
                (target_schema, "measurement"),
                (target_schema, "concept"),
            )
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            con.exec_driver_sql(
                f"""
                IF OBJECT_ID('tempdb..#obsclin_unit_reconcile') IS NOT NULL
                    DROP TABLE #obsclin_unit_reconcile;

                WITH unit_candidates AS (
                    SELECT
                        c.concept_code COLLATE Latin1_General_100_BIN2
                            AS concept_code,
                        c.concept_id,
                        COUNT_BIG(*) OVER (
                            PARTITION BY
                                c.concept_code COLLATE Latin1_General_100_BIN2
                        ) AS candidate_count
                    FROM [{target_schema}].[concept] c
                    WHERE c.vocabulary_id = 'UCUM'
                      AND c.domain_id = 'Unit'
                      AND c.standard_concept = 'S'
                      AND c.invalid_reason IS NULL
                ),
                unit_map AS (
                    SELECT
                        concept_code,
                        MAX(CASE WHEN candidate_count = 1
                                 THEN concept_id ELSE NULL END)
                            AS desired_unit_concept_id
                    FROM unit_candidates
                    GROUP BY concept_code
                )
                SELECT
                    m.measurement_id,
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(50), o.OBSCLIN_RESULT_UNIT
                    ))), '') AS source_unit,
                    m.unit_concept_id AS before_unit_concept_id,
                    m.unit_source_concept_id AS before_unit_source_concept_id,
                    COALESCE(um.desired_unit_concept_id, 0)
                        AS desired_unit_concept_id
                INTO #obsclin_unit_reconcile
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[etl_measurement_xwalk] x
                  ON x.measurement_id = m.measurement_id
                 AND x.source_family = 'OBS_CLIN'
                JOIN [{source_schema}].[PCORnet_OBS_CLIN] o
                  ON LTRIM(RTRIM(CONVERT(
                       nvarchar(255), o.OBSCLINID
                     ))) = x.source_record_id
                LEFT JOIN unit_map um
                  ON um.concept_code =
                     NULLIF(LTRIM(RTRIM(CONVERT(
                       nvarchar(50), o.OBSCLIN_RESULT_UNIT
                     ))), '') COLLATE Latin1_General_100_BIN2;
                """
            )

            summary = con.execute(
                text(
                    """
                    SELECT
                        COUNT_BIG(*) AS obsclin_measurement_rows,
                        SUM(CASE WHEN source_unit IS NOT NULL THEN 1 ELSE 0 END)
                            AS nonblank_source_unit_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id <>
                                   desired_unit_concept_id
                                OR before_unit_source_concept_id <>
                                   desired_unit_concept_id
                              THEN 1 ELSE 0 END) AS would_change_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id <> 0
                               AND desired_unit_concept_id = 0
                              THEN 1 ELSE 0 END) AS would_change_to_zero_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id = 0
                               AND desired_unit_concept_id <> 0
                              THEN 1 ELSE 0 END) AS would_change_to_nonzero_rows
                    FROM #obsclin_unit_reconcile
                    """
                )
            ).mappings().one()

            changed_units = [
                {
                    "source_unit": r[0],
                    "before_unit_concept_id": int(r[1]),
                    "before_unit_source_concept_id": int(r[2]),
                    "desired_unit_concept_id": int(r[3]),
                    "rows": int(r[4]),
                }
                for r in con.execute(
                    text(
                        """
                        SELECT TOP (50)
                            source_unit,
                            before_unit_concept_id,
                            before_unit_source_concept_id,
                            desired_unit_concept_id,
                            COUNT_BIG(*) AS n
                        FROM #obsclin_unit_reconcile
                        WHERE before_unit_concept_id <> desired_unit_concept_id
                           OR before_unit_source_concept_id <>
                              desired_unit_concept_id
                        GROUP BY
                            source_unit,
                            before_unit_concept_id,
                            before_unit_source_concept_id,
                            desired_unit_concept_id
                        ORDER BY COUNT_BIG(*) DESC, source_unit
                        """
                    )
                ).fetchall()
            ]

            updated_rows = 0
            if apply:
                result = con.execute(
                    text(
                        f"""
                        UPDATE m
                           SET m.unit_concept_id = r.desired_unit_concept_id,
                               m.unit_source_concept_id = r.desired_unit_concept_id
                        FROM [{target_schema}].[measurement] m
                        JOIN #obsclin_unit_reconcile r
                          ON r.measurement_id = m.measurement_id
                        WHERE m.unit_concept_id <> r.desired_unit_concept_id
                           OR m.unit_source_concept_id <>
                              r.desired_unit_concept_id
                        """
                    )
                )
                updated_rows = int(result.rowcount or 0)
                con.commit()

            after_mismatch_rows = int(
                con.execute(
                    text(
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[measurement] m
                        JOIN #obsclin_unit_reconcile r
                          ON r.measurement_id = m.measurement_id
                        WHERE m.unit_concept_id <> r.desired_unit_concept_id
                           OR m.unit_source_concept_id <>
                              r.desired_unit_concept_id
                        """
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()

    payload = {
        "stage": "obs_clin_unit_reconcile",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry_run",
        "rule": (
            "OBSCLIN_RESULT_UNIT maps only to an exact case-sensitive, unique, "
            "active Standard UCUM Unit concept; otherwise concept_id=0 and "
            "the source unit string is preserved."
        ),
        "obsclin_measurement_rows": int(
            summary["obsclin_measurement_rows"] or 0
        ),
        "nonblank_source_unit_rows": int(
            summary["nonblank_source_unit_rows"] or 0
        ),
        "would_change_rows": int(summary["would_change_rows"] or 0),
        "would_change_to_zero_rows": int(
            summary["would_change_to_zero_rows"] or 0
        ),
        "would_change_to_nonzero_rows": int(
            summary["would_change_to_nonzero_rows"] or 0
        ),
        "updated_rows": updated_rows,
        "after_mismatch_rows": after_mismatch_rows,
        "top_changed_units": changed_units,
        "status": (
            "matched"
            if apply and after_mismatch_rows == 0
            else "review_required"
            if not apply
            else "mismatch"
        ),
    }

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    payload["audit_path"] = str(audit_path)
    return payload
