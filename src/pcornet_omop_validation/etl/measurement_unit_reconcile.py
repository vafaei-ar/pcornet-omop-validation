from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_measurement_xwalk"


def reconcile_lab_units(config: EtlConfig, apply: bool = False) -> dict:
    """Reconcile LAB-derived Measurement units to exact UCUM semantics.

    UCUM concept-code matching is explicitly case-sensitive and only a unique,
    active, Standard Unit concept is accepted. Unresolved units remain concept
    0 while their source strings are preserved. This stage is intended only to
    reconcile an already-materialized validated database after correcting the
    primary ETL rule; a fresh ETL run should not require it.
    """
    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "measurement_unit_reconcile.json"

    engine = make_engine(config)
    try:
        with engine.connect() as con:
            required = (
                (source_schema, "PCORnet_LAB_RESULT_CM"),
                (source_schema, XWALK_TABLE),
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
                IF OBJECT_ID('tempdb..#lab_unit_reconcile') IS NOT NULL
                    DROP TABLE #lab_unit_reconcile;

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
                        MAX(
                            CASE WHEN candidate_count = 1
                                 THEN concept_id ELSE NULL END
                        ) AS unit_concept_id
                    FROM unit_candidates
                    GROUP BY concept_code
                )
                SELECT
                    m.measurement_id,
                    NULLIF(LTRIM(RTRIM(CONVERT(
                        nvarchar(50), l.RESULT_UNIT
                    ))), '') AS source_unit,
                    m.unit_concept_id AS before_unit_concept_id,
                    m.unit_source_concept_id AS before_unit_source_concept_id,
                    COALESCE(um.unit_concept_id, 0) AS desired_unit_concept_id
                INTO #lab_unit_reconcile
                FROM [{target_schema}].[measurement] m
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.measurement_id = m.measurement_id
                 AND x.source_family = 'LAB_RESULT_CM'
                JOIN [{source_schema}].[PCORnet_LAB_RESULT_CM] l
                  ON LTRIM(RTRIM(CONVERT(
                       nvarchar(255), l.LAB_RESULT_CM_ID
                     ))) = x.source_record_id
                LEFT JOIN unit_map um
                  ON um.concept_code =
                     NULLIF(LTRIM(RTRIM(CONVERT(
                       nvarchar(50), l.RESULT_UNIT
                     ))), '') COLLATE Latin1_General_100_BIN2;
                """
            )

            summary = con.execute(
                text(
                    """
                    SELECT
                        COUNT_BIG(*) AS lab_rows,
                        SUM(CASE WHEN source_unit IS NOT NULL THEN 1 ELSE 0 END)
                            AS nonblank_source_unit_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id <> desired_unit_concept_id
                                OR before_unit_source_concept_id <> desired_unit_concept_id
                              THEN 1 ELSE 0 END) AS would_change_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id <> 0
                               AND desired_unit_concept_id = 0
                              THEN 1 ELSE 0 END) AS would_change_to_zero_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id = 0
                               AND desired_unit_concept_id <> 0
                              THEN 1 ELSE 0 END) AS would_change_to_nonzero_rows,
                        SUM(CASE
                              WHEN before_unit_concept_id <> 0
                               AND before_unit_concept_id <> desired_unit_concept_id
                              THEN 1 ELSE 0 END) AS incorrect_nonzero_before_rows
                    FROM #lab_unit_reconcile
                    """
                )
            ).mappings().one()

            changed_units = [
                {
                    "source_unit": r[0],
                    "before_unit_concept_id": int(r[1]),
                    "desired_unit_concept_id": int(r[2]),
                    "rows": int(r[3]),
                }
                for r in con.execute(
                    text(
                        """
                        SELECT TOP (50)
                            source_unit,
                            before_unit_concept_id,
                            desired_unit_concept_id,
                            COUNT_BIG(*) AS n
                        FROM #lab_unit_reconcile
                        WHERE before_unit_concept_id <> desired_unit_concept_id
                        GROUP BY
                            source_unit,
                            before_unit_concept_id,
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
                        JOIN #lab_unit_reconcile r
                          ON r.measurement_id = m.measurement_id
                        WHERE m.unit_concept_id <> r.desired_unit_concept_id
                           OR m.unit_source_concept_id <> r.desired_unit_concept_id
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
                        JOIN #lab_unit_reconcile r
                          ON r.measurement_id = m.measurement_id
                        WHERE m.unit_concept_id <> r.desired_unit_concept_id
                           OR m.unit_source_concept_id <> r.desired_unit_concept_id
                        """
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()

    payload = {
        "stage": "measurement_unit_reconcile",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry_run",
        "rule": (
            "LAB RESULT_UNIT maps only to an exact case-sensitive, unique, "
            "active Standard UCUM Unit concept; otherwise concept_id=0 and "
            "unit_source_value is preserved."
        ),
        "lab_rows": int(summary["lab_rows"] or 0),
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
        "incorrect_nonzero_before_rows": int(
            summary["incorrect_nonzero_before_rows"] or 0
        ),
        "updated_rows": updated_rows,
        "after_mismatch_rows": after_mismatch_rows,
        "top_changed_units": changed_units,
        "status": (
            "matched"
            if (apply and after_mismatch_rows == 0)
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
