from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


TARGET_TABLES = (
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
    "specimen",
    "death",
)

LINEAGE_TO_TARGET = {
    "etl_condition_occurrence_xwalk": "condition_occurrence",
    "etl_procedure_occurrence_xwalk": "procedure_occurrence",
    "etl_measurement_xwalk": "measurement",
    "etl_observation_xwalk": "observation",
    "etl_drug_exposure_xwalk": "drug_exposure",
    "etl_device_exposure_xwalk": "device_exposure",
    "etl_specimen_xwalk": "specimen",
    "etl_death_xwalk": "death",
}

ROUTE_TABLES = (
    "etl_procedure_event_route",
    "etl_obs_clin_route",
    "etl_drug_event_route",
)

CORE_REQUIRED_DATES = {
    "observation_period": ("observation_period_start_date", "observation_period_end_date"),
    "visit_occurrence": ("visit_start_date", "visit_end_date"),
    "condition_occurrence": ("condition_start_date",),
    "procedure_occurrence": ("procedure_date",),
    "measurement": ("measurement_date",),
    "observation": ("observation_date",),
    "drug_exposure": ("drug_exposure_start_date", "drug_exposure_end_date"),
    "device_exposure": ("device_exposure_start_date",),
    "specimen": ("specimen_date",),
    "death": ("death_date",),
}

INTERVAL_CHECKS = {
    "observation_period": ("observation_period_start_date", "observation_period_end_date"),
    "visit_occurrence": ("visit_start_date", "visit_end_date"),
    "condition_occurrence": ("condition_start_date", "condition_end_date"),
    "drug_exposure": ("drug_exposure_start_date", "drug_exposure_end_date"),
    "device_exposure": ("device_exposure_start_date", "device_exposure_end_date"),
}

CONCEPT_COLUMNS = {
    "person": ("gender_concept_id", "race_concept_id", "ethnicity_concept_id"),
    "visit_occurrence": ("visit_concept_id", "visit_type_concept_id"),
    "condition_occurrence": (
        "condition_concept_id",
        "condition_type_concept_id",
        "condition_status_concept_id",
    ),
    "procedure_occurrence": ("procedure_concept_id", "procedure_type_concept_id"),
    "measurement": (
        "measurement_concept_id",
        "measurement_type_concept_id",
        "operator_concept_id",
        "value_as_concept_id",
        "unit_concept_id",
    ),
    "observation": (
        "observation_concept_id",
        "observation_type_concept_id",
        "value_as_concept_id",
        "qualifier_concept_id",
        "unit_concept_id",
    ),
    "drug_exposure": ("drug_concept_id", "drug_type_concept_id", "route_concept_id"),
    "device_exposure": ("device_concept_id", "device_type_concept_id", "unit_concept_id"),
    "specimen": (
        "specimen_concept_id",
        "specimen_type_concept_id",
        "unit_concept_id",
        "anatomic_site_concept_id",
        "disease_status_concept_id",
    ),
    "death": ("death_type_concept_id", "cause_concept_id"),
}

PRIMARY_KEYS = {
    "person": "person_id",
    "observation_period": "observation_period_id",
    "visit_occurrence": "visit_occurrence_id",
    "condition_occurrence": "condition_occurrence_id",
    "procedure_occurrence": "procedure_occurrence_id",
    "measurement": "measurement_id",
    "observation": "observation_id",
    "drug_exposure": "drug_exposure_id",
    "device_exposure": "device_exposure_id",
    "specimen": "specimen_id",
}

VISIT_LINKED_TABLES = (
    "condition_occurrence",
    "procedure_occurrence",
    "measurement",
    "observation",
    "drug_exposure",
    "device_exposure",
)


def _schema(value: object, label: str) -> str:
    schema = str(value or "dbo")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError(f"Unsafe SQL Server {label}: {schema!r}")
    return schema


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def reconcile_validated_etl(config: EtlConfig) -> dict[str, object]:
    """Reconcile the materialized validated ETL without dataset-specific counts.

    The audit verifies internal completeness, required dates, interval validity,
    lineage-to-target equality, route-ledger structure, concept-zero profiles,
    visit linkage, and duplicate primary identifiers. Row counts are observations
    of the current run, not transformation rules.
    """
    sql_cfg = config.raw["sqlserver"]
    target_schema = _schema(sql_cfg.get("target_schema", "dbo"), "target_schema")
    audit_path = config.audit_dir / "global_reconciliation.json"

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            for table in TARGET_TABLES:
                if not table_exists(connection, target_schema, table):
                    raise RuntimeError(
                        f"Required target table [{target_schema}].[{table}] is missing"
                    )

            for table in (*LINEAGE_TO_TARGET, *ROUTE_TABLES):
                if not table_exists(connection, target_schema, table):
                    raise RuntimeError(
                        f"Required ETL ledger [{target_schema}].[{table}] is missing"
                    )

            target_rows = {
                table: _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]",
                )
                for table in TARGET_TABLES
            }

            lineage_rows: dict[str, int] = {}
            lineage_reconciliation: dict[str, dict[str, int | bool]] = {}
            for xwalk, target in LINEAGE_TO_TARGET.items():
                lineage = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{xwalk}]",
                )
                expected = target_rows[target]
                lineage_rows[xwalk] = lineage
                lineage_reconciliation[xwalk] = {
                    "target_rows": expected,
                    "lineage_rows": lineage,
                    "matched": lineage == expected,
                }
                if lineage != expected:
                    raise RuntimeError(
                        f"Lineage reconciliation failed for {xwalk}: "
                        f"lineage={lineage:,}, {target}={expected:,}"
                    )

            route_rows = {
                table: _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{table}]",
                )
                for table in ROUTE_TABLES
            }

            required_date_nulls: dict[str, dict[str, int]] = {}
            for table, columns in CORE_REQUIRED_DATES.items():
                required_date_nulls[table] = {}
                for column in columns:
                    n = _scalar(
                        connection,
                        f"""
                        SELECT COUNT_BIG(*)
                        FROM [{target_schema}].[{table}]
                        WHERE [{column}] IS NULL
                        """,
                    )
                    required_date_nulls[table][column] = n
                    if n:
                        raise RuntimeError(
                            f"Required date NULLs found in {table}.{column}: {n:,}"
                        )

            reversed_intervals: dict[str, int] = {}
            for table, (start_col, end_col) in INTERVAL_CHECKS.items():
                n = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}]
                    WHERE [{end_col}] IS NOT NULL
                      AND [{end_col}] < [{start_col}]
                    """,
                )
                reversed_intervals[table] = n
                if n:
                    raise RuntimeError(
                        f"Reversed target intervals found in {table}: {n:,}"
                    )

            concept_zero: dict[str, dict[str, int]] = {}
            for table, columns in CONCEPT_COLUMNS.items():
                present = {
                    str(row[0])
                    for row in connection.execute(
                        text(
                            """
                            SELECT c.name
                            FROM sys.columns c
                            WHERE c.object_id = OBJECT_ID(:obj)
                            """
                        ),
                        {"obj": f"{target_schema}.{table}"},
                    ).fetchall()
                }
                concept_zero[table] = {}
                for column in columns:
                    if column in present:
                        concept_zero[table][column] = _scalar(
                            connection,
                            f"""
                            SELECT COUNT_BIG(*)
                            FROM [{target_schema}].[{table}]
                            WHERE COALESCE([{column}], 0) = 0
                            """,
                        )

            visit_linkage: dict[str, dict[str, int]] = {}
            for table in VISIT_LINKED_TABLES:
                linked = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM [{target_schema}].[{table}]
                    WHERE visit_occurrence_id IS NOT NULL
                    """,
                )
                visit_linkage[table] = {
                    "linked": linked,
                    "unlinked": target_rows[table] - linked,
                }

            procedure_domain_totals = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    text(
                        f"""
                        SELECT target_domain, COUNT_BIG(*)
                        FROM [{target_schema}].[etl_procedure_event_route]
                        GROUP BY target_domain
                        ORDER BY target_domain
                        """
                    )
                ).fetchall()
            }
            obs_clin_domain_totals = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    text(
                        f"""
                        SELECT target_domain, COUNT_BIG(*)
                        FROM [{target_schema}].[etl_obs_clin_route]
                        GROUP BY target_domain
                        ORDER BY target_domain
                        """
                    )
                ).fetchall()
            }
            drug_family_totals = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    text(
                        f"""
                        SELECT source_domain, COUNT_BIG(*)
                        FROM [{target_schema}].[etl_drug_event_route]
                        GROUP BY source_domain
                        ORDER BY source_domain
                        """
                    )
                ).fetchall()
            }

            duplicate_primary_keys: dict[str, int] = {}
            for table, column in PRIMARY_KEYS.items():
                n = _scalar(
                    connection,
                    f"""
                    SELECT COUNT_BIG(*)
                    FROM (
                        SELECT [{column}]
                        FROM [{target_schema}].[{table}]
                        GROUP BY [{column}]
                        HAVING COUNT_BIG(*) > 1
                    ) x
                    """,
                )
                duplicate_primary_keys[table] = n
                if n:
                    raise RuntimeError(
                        f"Duplicate primary IDs found in {table}: {n:,}"
                    )

        payload = {
            "stage": "global_reconciliation",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "target_rows": target_rows,
            "lineage_rows": lineage_rows,
            "lineage_reconciliation": lineage_reconciliation,
            "route_rows": route_rows,
            "required_date_nulls": required_date_nulls,
            "reversed_intervals": reversed_intervals,
            "concept_zero_rows": concept_zero,
            "visit_linkage": visit_linkage,
            "procedure_route_domain_totals": procedure_domain_totals,
            "obs_clin_route_domain_totals": obs_clin_domain_totals,
            "drug_route_family_totals": drug_family_totals,
            "duplicate_primary_keys": duplicate_primary_keys,
            "status": "matched",
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
