from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


PRIMARY_CONCEPTS = {
    "condition_occurrence": ("condition_concept_id", "Condition"),
    "procedure_occurrence": ("procedure_concept_id", "Procedure"),
    "measurement": ("measurement_concept_id", "Measurement"),
    "observation": ("observation_concept_id", "Observation"),
    "drug_exposure": ("drug_concept_id", "Drug"),
    "device_exposure": ("device_concept_id", "Device"),
    "specimen": ("specimen_concept_id", "Specimen"),
}

TYPE_CONCEPT_COLUMNS = {
    "condition_occurrence": "condition_type_concept_id",
    "procedure_occurrence": "procedure_type_concept_id",
    "measurement": "measurement_type_concept_id",
    "observation": "observation_type_concept_id",
    "drug_exposure": "drug_type_concept_id",
    "device_exposure": "device_type_concept_id",
    "specimen": "specimen_type_concept_id",
    "death": "death_type_concept_id",
}


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def _rows(connection, sql: str, params: dict[str, object] | None = None):
    return connection.execute(text(sql), params or {}).fetchall()


def audit_semantic_freeze(config: EtlConfig) -> dict[str, object]:
    sql_cfg = config.raw["sqlserver"]
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "semantic_freeze_audit.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            required = ["concept", *PRIMARY_CONCEPTS.keys(), "death"]
            for table in required:
                if not table_exists(connection, target_schema, table):
                    raise RuntimeError(
                        f"Required table [{target_schema}].[{table}] does not exist"
                    )

            primary_concept_checks: dict[str, dict[str, int]] = {}
            for table, (column, expected_domain) in PRIMARY_CONCEPTS.items():
                nonzero = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}] WHERE {column} <> 0",
                )
                missing_concept = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}] t
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = t.{column}
                    WHERE t.{column} <> 0
                      AND c.concept_id IS NULL
                    """,
                )
                invalid = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}] t
                    JOIN [{target_schema}].[concept] c
                      ON c.concept_id = t.{column}
                    WHERE t.{column} <> 0
                      AND c.invalid_reason IS NOT NULL
                    """,
                )
                nonstandard = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}] t
                    JOIN [{target_schema}].[concept] c
                      ON c.concept_id = t.{column}
                    WHERE t.{column} <> 0
                      AND COALESCE(c.standard_concept, '') <> 'S'
                    """,
                )
                wrong_domain = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}] t
                    JOIN [{target_schema}].[concept] c
                      ON c.concept_id = t.{column}
                    WHERE t.{column} <> 0
                      AND c.domain_id <> :expected_domain
                    """,
                    {"expected_domain": expected_domain},
                )
                primary_concept_checks[table] = {
                    "nonzero_rows": nonzero,
                    "missing_concept_rows": missing_concept,
                    "invalid_concept_rows": invalid,
                    "nonstandard_concept_rows": nonstandard,
                    "wrong_domain_rows": wrong_domain,
                }

            type_concept_checks: dict[str, dict[str, object]] = {}
            for table, column in TYPE_CONCEPT_COLUMNS.items():
                total = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]",
                )
                zero = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}] WHERE COALESCE({column}, 0) = 0",
                )
                bad_nonzero = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}] t
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = t.{column}
                    WHERE COALESCE(t.{column}, 0) <> 0
                      AND (
                           c.concept_id IS NULL
                        OR c.invalid_reason IS NOT NULL
                        OR c.domain_id <> 'Type Concept'
                      )
                    """,
                )
                distribution = [
                    {
                        "concept_id": int(row[0]) if row[0] is not None else 0,
                        "concept_name": row[1],
                        "n": int(row[2]),
                    }
                    for row in _rows(
                        connection,
                        f"""
                        SELECT TOP (25)
                            COALESCE(t.{column}, 0) AS concept_id,
                            c.concept_name,
                            COUNT_BIG(*) AS n
                        FROM [{target_schema}].[{table}] t
                        LEFT JOIN [{target_schema}].[concept] c
                          ON c.concept_id = t.{column}
                        GROUP BY COALESCE(t.{column}, 0), c.concept_name
                        ORDER BY n DESC, concept_id
                        """,
                    )
                ]
                type_concept_checks[table] = {
                    "total_rows": total,
                    "concept_zero_rows": zero,
                    "concept_zero_fraction": (zero / total) if total else 0.0,
                    "invalid_nonzero_rows": bad_nonzero,
                    "distribution": distribution,
                }

            measurement_unit_profile = [
                {
                    "unit_source_value": row[0],
                    "unit_concept_id": int(row[1]) if row[1] is not None else 0,
                    "unit_concept_name": row[2],
                    "n": int(row[3]),
                }
                for row in _rows(
                    connection,
                    f"""
                    SELECT TOP (100)
                        m.unit_source_value,
                        COALESCE(m.unit_concept_id, 0) AS unit_concept_id,
                        c.concept_name,
                        COUNT_BIG(*) AS n
                    FROM [{target_schema}].[measurement] m
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = m.unit_concept_id
                    GROUP BY
                        m.unit_source_value,
                        COALESCE(m.unit_concept_id, 0),
                        c.concept_name
                    ORDER BY n DESC, m.unit_source_value
                    """,
                )
            ]

            measurement_bad_unit_domain = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[measurement] m
                JOIN [{target_schema}].[concept] c
                  ON c.concept_id = m.unit_concept_id
                WHERE COALESCE(m.unit_concept_id, 0) <> 0
                  AND c.domain_id <> 'Unit'
                """,
            )

            drug_route_profile = [
                {
                    "route_source_value": row[0],
                    "route_concept_id": int(row[1]) if row[1] is not None else 0,
                    "route_concept_name": row[2],
                    "n": int(row[3]),
                }
                for row in _rows(
                    connection,
                    f"""
                    SELECT TOP (100)
                        d.route_source_value,
                        COALESCE(d.route_concept_id, 0) AS route_concept_id,
                        c.concept_name,
                        COUNT_BIG(*) AS n
                    FROM [{target_schema}].[drug_exposure] d
                    LEFT JOIN [{target_schema}].[concept] c
                      ON c.concept_id = d.route_concept_id
                    GROUP BY
                        d.route_source_value,
                        COALESCE(d.route_concept_id, 0),
                        c.concept_name
                    ORDER BY n DESC, d.route_source_value
                    """,
                )
            ]

            drug_nonnull_route_source_zero = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[drug_exposure]
                WHERE NULLIF(LTRIM(RTRIM(route_source_value)), '') IS NOT NULL
                  AND COALESCE(route_concept_id, 0) = 0
                """,
            )

            observation_value_profile = [
                {
                    "observation_concept_id": int(row[0]),
                    "observation_name": row[1],
                    "value_as_string": row[2],
                    "value_as_concept_id": int(row[3]) if row[3] is not None else 0,
                    "value_concept_name": row[4],
                    "n": int(row[5]),
                }
                for row in _rows(
                    connection,
                    f"""
                    SELECT TOP (100)
                        o.observation_concept_id,
                        oc.concept_name,
                        o.value_as_string,
                        COALESCE(o.value_as_concept_id, 0),
                        vc.concept_name,
                        COUNT_BIG(*) AS n
                    FROM [{target_schema}].[observation] o
                    LEFT JOIN [{target_schema}].[concept] oc
                      ON oc.concept_id = o.observation_concept_id
                    LEFT JOIN [{target_schema}].[concept] vc
                      ON vc.concept_id = o.value_as_concept_id
                    WHERE NULLIF(LTRIM(RTRIM(o.value_as_string)), '') IS NOT NULL
                    GROUP BY
                        o.observation_concept_id,
                        oc.concept_name,
                        o.value_as_string,
                        COALESCE(o.value_as_concept_id, 0),
                        vc.concept_name
                    ORDER BY n DESC
                    """,
                )
            ]

            death_type_zero = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death] WHERE COALESCE(death_type_concept_id, 0) = 0",
            )

            blockers = []
            for table, checks in primary_concept_checks.items():
                for key in (
                    "missing_concept_rows",
                    "invalid_concept_rows",
                    "nonstandard_concept_rows",
                    "wrong_domain_rows",
                ):
                    if checks[key] != 0:
                        blockers.append(f"{table}.{key}={checks[key]}")
            if measurement_bad_unit_domain:
                blockers.append(
                    f"measurement.bad_unit_domain_rows={measurement_bad_unit_domain}"
                )

            review_flags = []
            for table, checks in type_concept_checks.items():
                if checks["concept_zero_rows"]:
                    review_flags.append(
                        f"{table} has {checks['concept_zero_rows']:,} rows with type concept 0"
                    )
            if drug_nonnull_route_source_zero:
                review_flags.append(
                    f"drug_exposure has {drug_nonnull_route_source_zero:,} rows with nonblank route source but route concept 0"
                )
            if death_type_zero:
                review_flags.append(
                    f"death has {death_type_zero:,} rows with death_type_concept_id 0 by explicit provenance policy"
                )

        payload = {
            "stage": "semantic_freeze_audit",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "primary_concept_checks": primary_concept_checks,
            "type_concept_checks": type_concept_checks,
            "measurement_bad_unit_domain_rows": measurement_bad_unit_domain,
            "measurement_unit_profile_top100": measurement_unit_profile,
            "drug_nonblank_route_source_concept_zero_rows": drug_nonnull_route_source_zero,
            "drug_route_profile_top100": drug_route_profile,
            "observation_value_profile_top100": observation_value_profile,
            "hard_blockers": blockers,
            "review_flags": review_flags,
            "status": "blocked" if blockers else "review_required",
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
