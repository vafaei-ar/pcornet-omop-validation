from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


XWALK_TABLE = "etl_death_xwalk"


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def transform_death(config: EtlConfig) -> dict[str, object]:
    policies = config.raw.get("policies", {}) or {}
    if policies.get("missing_required_date") != "exclude":
        raise RuntimeError(
            "Validated death ETL requires policies.missing_required_date=exclude"
        )

    sql_cfg = config.raw["sqlserver"]
    source_schema = str(sql_cfg.get("source_schema", "dbo"))
    target_schema = str(sql_cfg.get("target_schema", "dbo"))
    audit_path = config.audit_dir / "death_transform.json"

    engine = make_engine(config)
    try:
        with engine.begin() as connection:
            required = (
                (source_schema, "PCORnet_DEATH"),
                (source_schema, "PCORnet_DEATH_CAUSE"),
                (target_schema, "person"),
                (target_schema, "death"),
            )
            for schema, table in required:
                if not table_exists(connection, schema, table):
                    raise RuntimeError(
                        f"Required table [{schema}].[{table}] does not exist"
                    )

            source_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_DEATH]",
            )
            cause_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_DEATH_CAUSE]",
            )
            if cause_rows:
                raise RuntimeError(
                    "PCORnet_DEATH_CAUSE contains rows, but validated cause-of-death "
                    "routing is not implemented. Refusing silent information loss."
                )

            duplicate_patids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM (
                  SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) AS patid
                  FROM [{source_schema}].[PCORnet_DEATH]
                  WHERE PATID IS NOT NULL
                    AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
                  GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), PATID)))
                  HAVING COUNT_BIG(*) > 1
                ) x
                """,
            )
            if duplicate_patids:
                raise RuntimeError(
                    f"Duplicate PATIDs in PCORnet_DEATH: {duplicate_patids:,}"
                )

            excluded_missing_patid = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_DEATH]
                WHERE PATID IS NULL
                   OR LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) = ''
                """,
            )
            excluded_missing_date = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_DEATH]
                WHERE PATID IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) <> ''
                  AND DEATH_DATE IS NULL
                """,
            )
            excluded_unlinked_person = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_DEATH] d
                LEFT JOIN [{target_schema}].[person] p
                  ON p.person_source_value =
                     LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                WHERE d.PATID IS NOT NULL
                  AND LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) <> ''
                  AND d.DEATH_DATE IS NOT NULL
                  AND p.person_id IS NULL
                """,
            )
            eligible_rows = (
                source_rows
                - excluded_missing_patid
                - excluded_missing_date
                - excluded_unlinked_person
            )

            source_profile = [
                {
                    "death_source": row[0],
                    "death_date_impute": row[1],
                    "rows": int(row[2]),
                }
                for row in connection.execute(
                    text(
                        f"""
                        SELECT
                          COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_SOURCE))), ''),
                            '<NULL>'
                          ) AS death_source,
                          COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_DATE_IMPUTE))), ''),
                            '<NULL>'
                          ) AS death_date_impute,
                          COUNT_BIG(*) AS n
                        FROM [{source_schema}].[PCORnet_DEATH]
                        GROUP BY
                          COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_SOURCE))), ''),
                            '<NULL>'
                          ),
                          COALESCE(
                            NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_DATE_IMPUTE))), ''),
                            '<NULL>'
                          )
                        ORDER BY n DESC, death_source, death_date_impute
                        """
                    )
                ).fetchall()
            ]

            current = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death]",
            )
            xwalk_exists = table_exists(connection, target_schema, XWALK_TABLE)

            if current == eligible_rows and xwalk_exists:
                xwalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]",
                )
                if xwalk_rows != eligible_rows:
                    raise RuntimeError(
                        "death is populated but death lineage is not reconciled"
                    )
                status = "already_matched"
            elif current != 0 or xwalk_exists:
                raise RuntimeError(
                    f"Unexpected pre-transform state: death_rows={current:,}, "
                    f"xwalk_exists={int(xwalk_exists)}"
                )
            else:
                connection.execute(
                    text(
                        f"""
                        INSERT INTO [{target_schema}].[death] (
                          person_id, death_date, death_datetime,
                          death_type_concept_id, cause_concept_id,
                          cause_source_value, cause_source_concept_id
                        )
                        SELECT
                          p.person_id,
                          CAST(d.DEATH_DATE AS date),
                          CAST(d.DEATH_DATE AS datetime2(7)),
                          0, 0, NULL, 0
                        FROM [{source_schema}].[PCORnet_DEATH] d
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value =
                             LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                        WHERE d.PATID IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) <> ''
                          AND d.DEATH_DATE IS NOT NULL
                        """
                    )
                )
                connection.execute(
                    text(
                        f"""
                        CREATE TABLE [{target_schema}].[{XWALK_TABLE}] (
                          person_id int NOT NULL PRIMARY KEY,
                          source_patid nvarchar(255) NOT NULL UNIQUE,
                          death_source nvarchar(50) NULL,
                          death_date_impute nvarchar(50) NULL,
                          death_match_confidence nvarchar(50) NULL,
                          source_death_date date NOT NULL,
                          death_type_mapping_basis varchar(256) NOT NULL,
                          cause_mapping_basis varchar(256) NOT NULL
                        );

                        INSERT INTO [{target_schema}].[{XWALK_TABLE}] (
                          person_id, source_patid, death_source,
                          death_date_impute, death_match_confidence,
                          source_death_date, death_type_mapping_basis,
                          cause_mapping_basis
                        )
                        SELECT
                          p.person_id,
                          LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))),
                          LEFT(CONVERT(nvarchar(50), d.DEATH_SOURCE), 50),
                          LEFT(CONVERT(nvarchar(50), d.DEATH_DATE_IMPUTE), 50),
                          LEFT(CONVERT(nvarchar(50), d.DEATH_MATCH_CONFIDENCE), 50),
                          CAST(d.DEATH_DATE AS date),
                          'No exact OMOP Death Type concept is inferred from PCORnet DEATH_SOURCE; use concept 0 and preserve source provenance',
                          'No PCORnet_DEATH_CAUSE rows were present; cause remains concept 0'
                        FROM [{source_schema}].[PCORnet_DEATH] d
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value =
                             LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                        WHERE d.PATID IS NOT NULL
                          AND LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) <> ''
                          AND d.DEATH_DATE IS NOT NULL
                        """
                    )
                )
                status = "matched"

            target_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death]",
            )
            xwalk_rows = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[{XWALK_TABLE}]",
            )
            source_date_matches = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[death] d
                JOIN [{target_schema}].[{XWALK_TABLE}] x
                  ON x.person_id = d.person_id
                WHERE d.death_date = x.source_death_date
                """,
            )
            concept_zero_type_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[death]
                WHERE death_type_concept_id = 0
                """,
            )
            cause_zero_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[death]
                WHERE COALESCE(cause_concept_id, 0) = 0
                  AND COALESCE(cause_source_concept_id, 0) = 0
                  AND cause_source_value IS NULL
                """,
            )

            checks = {
                "eligible_vs_target": target_rows == eligible_rows,
                "eligible_vs_lineage": xwalk_rows == eligible_rows,
                "exact_date_fidelity": source_date_matches == eligible_rows,
                "death_type_policy": concept_zero_type_rows == eligible_rows,
                "cause_policy": cause_zero_rows == eligible_rows,
            }
            if not all(checks.values()):
                raise RuntimeError(f"Death reconciliation failed: {checks}")

        payload = {
            "stage": "death_transform",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_rows": source_rows,
            "eligible_rows": eligible_rows,
            "excluded_missing_patid": excluded_missing_patid,
            "excluded_missing_death_date": excluded_missing_date,
            "excluded_unlinked_person": excluded_unlinked_person,
            "death_cause_rows": cause_rows,
            "target_rows": target_rows,
            "lineage_rows": xwalk_rows,
            "exact_death_date_matches": source_date_matches,
            "death_type_concept_zero_rows": concept_zero_type_rows,
            "cause_concept_zero_rows": cause_zero_rows,
            "source_profile": source_profile,
            "checks": checks,
            "death_type_policy": (
                "Preserve DEATH_SOURCE and DEATH_DATE_IMPUTE in lineage. Do not "
                "infer a specific OMOP Death Type concept without an exact, "
                "prespecified provenance mapping; use death_type_concept_id=0."
            ),
            "death_date_policy": (
                "Require a source DEATH_DATE. Missing dates are excluded and "
                "quantified; present dates are preserved exactly, including "
                "source-reported imputation status in lineage."
            ),
            "cause_policy": (
                "Fail closed when PCORnet_DEATH_CAUSE contains rows until a "
                "validated cause-routing implementation is available; never "
                "silently discard cause information."
            ),
            "status": status,
        }
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
