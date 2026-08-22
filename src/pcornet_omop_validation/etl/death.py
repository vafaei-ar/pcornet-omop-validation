from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


EXPECTED_DEATH_ROWS = 6_955
EXPECTED_DEATH_CAUSE_ROWS = 0
EXPECTED_DEATH_SOURCE = "L"
EXPECTED_DEATH_DATE_IMPUTE = "N"

XWALK_TABLE = "etl_death_xwalk"


def _scalar(connection, sql: str, params: dict[str, object] | None = None) -> int:
    return int(connection.execute(text(sql), params or {}).scalar_one())


def transform_death(config: EtlConfig) -> dict[str, object]:
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
            if source_rows != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    f"PCORnet_DEATH count changed: {source_rows:,} != {EXPECTED_DEATH_ROWS:,}"
                )
            if cause_rows != EXPECTED_DEATH_CAUSE_ROWS:
                raise RuntimeError(
                    f"PCORnet_DEATH_CAUSE is no longer empty: {cause_rows:,} rows"
                )

            invalid_rows = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{source_schema}].[PCORnet_DEATH] d
                LEFT JOIN [{target_schema}].[person] p
                  ON p.person_source_value = LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                WHERE d.PATID IS NULL
                   OR LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))) = ''
                   OR d.DEATH_DATE IS NULL
                   OR p.person_id IS NULL
                """,
            )
            if invalid_rows != 0:
                raise RuntimeError(
                    f"Invalid/unlinked DEATH rows found: {invalid_rows:,}"
                )

            duplicate_patids = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM (
                    SELECT LTRIM(RTRIM(CONVERT(nvarchar(255), PATID))) AS patid
                    FROM [{source_schema}].[PCORnet_DEATH]
                    GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(255), PATID)))
                    HAVING COUNT_BIG(*) > 1
                ) x
                """,
            )
            if duplicate_patids != 0:
                raise RuntimeError(
                    f"Duplicate PATIDs in PCORnet_DEATH: {duplicate_patids:,}"
                )

            source_profile = dict(
                connection.execute(
                    text(
                        f"""
                        SELECT
                            CONCAT(
                                COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_SOURCE))), ''), '<NULL>'),
                                '|',
                                COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_DATE_IMPUTE))), ''), '<NULL>')
                            ) AS source_impute,
                            COUNT_BIG(*) AS n
                        FROM [{source_schema}].[PCORnet_DEATH]
                        GROUP BY
                            COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_SOURCE))), ''), '<NULL>'),
                            COALESCE(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(50), DEATH_DATE_IMPUTE))), ''), '<NULL>')
                        """
                    )
                ).all()
            )
            expected_profile = {
                f"{EXPECTED_DEATH_SOURCE}|{EXPECTED_DEATH_DATE_IMPUTE}": EXPECTED_DEATH_ROWS
            }
            if source_profile != expected_profile:
                raise RuntimeError(
                    f"Unexpected DEATH source/imputation profile: {source_profile!r}"
                )

            current = _scalar(
                connection,
                f"SELECT COUNT_BIG(*) FROM [{target_schema}].[death]",
            )
            xwalk_exists = table_exists(connection, source_schema, XWALK_TABLE)

            if current == EXPECTED_DEATH_ROWS and xwalk_exists:
                xwalk_rows = _scalar(
                    connection,
                    f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{XWALK_TABLE}]",
                )
                if xwalk_rows == EXPECTED_DEATH_ROWS:
                    status = "already_matched"
                else:
                    raise RuntimeError(
                        "death is populated but death lineage is not reconciled"
                    )
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
                            person_id,
                            death_date,
                            death_datetime,
                            death_type_concept_id,
                            cause_concept_id,
                            cause_source_value,
                            cause_source_concept_id
                        )
                        SELECT
                            p.person_id,
                            CAST(d.DEATH_DATE AS date),
                            CAST(d.DEATH_DATE AS datetime),
                            0,
                            0,
                            NULL,
                            0
                        FROM [{source_schema}].[PCORnet_DEATH] d
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value =
                             LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)))
                        """
                    )
                )

                connection.execute(
                    text(
                        f"""
                        CREATE TABLE [{source_schema}].[{XWALK_TABLE}] (
                            person_id int NOT NULL PRIMARY KEY,
                            source_patid nvarchar(255) NOT NULL UNIQUE,
                            death_source nvarchar(50) NULL,
                            death_date_impute nvarchar(50) NULL,
                            death_match_confidence nvarchar(50) NULL,
                            source_death_date date NOT NULL,
                            death_type_mapping_basis varchar(128) NOT NULL,
                            cause_mapping_basis varchar(128) NOT NULL
                        );

                        INSERT INTO [{source_schema}].[{XWALK_TABLE}] (
                            person_id,
                            source_patid,
                            death_source,
                            death_date_impute,
                            death_match_confidence,
                            source_death_date,
                            death_type_mapping_basis,
                            cause_mapping_basis
                        )
                        SELECT
                            p.person_id,
                            LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID))),
                            LEFT(CONVERT(nvarchar(50), d.DEATH_SOURCE), 50),
                            LEFT(CONVERT(nvarchar(50), d.DEATH_DATE_IMPUTE), 50),
                            LEFT(CONVERT(nvarchar(50), d.DEATH_MATCH_CONFIDENCE), 50),
                            CAST(d.DEATH_DATE AS date),
                            'DEATH_SOURCE=L has no precise frozen OMOP Death Type mapping; use concept 0 and preserve source lineage',
                            'PCORnet_DEATH_CAUSE empty; no cause available'
                        FROM [{source_schema}].[PCORnet_DEATH] d
                        JOIN [{target_schema}].[person] p
                          ON p.person_source_value =
                             LTRIM(RTRIM(CONVERT(nvarchar(255), d.PATID)));
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
                f"SELECT COUNT_BIG(*) FROM [{source_schema}].[{XWALK_TABLE}]",
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
                """,
            )
            source_date_matches = _scalar(
                connection,
                f"""
                SELECT COUNT_BIG(*)
                FROM [{target_schema}].[death] od
                JOIN [{source_schema}].[{XWALK_TABLE}] x
                  ON x.person_id = od.person_id
                WHERE od.death_date = x.source_death_date
                """,
            )

            if target_rows != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    f"Death target count mismatch: {target_rows:,} != {EXPECTED_DEATH_ROWS:,}"
                )
            if xwalk_rows != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    f"Death lineage count mismatch: {xwalk_rows:,} != {EXPECTED_DEATH_ROWS:,}"
                )
            if source_date_matches != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    f"Death date fidelity mismatch: {source_date_matches:,} exact matches"
                )
            if concept_zero_type_rows != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    "Death type policy drifted; expected all local-source deaths to remain concept 0"
                )
            if cause_zero_rows != EXPECTED_DEATH_ROWS:
                raise RuntimeError(
                    "Cause-of-death policy drifted despite empty PCORnet_DEATH_CAUSE"
                )

        payload = {
            "stage": "death_transform",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_rows": source_rows,
            "death_cause_rows": cause_rows,
            "target_rows": target_rows,
            "lineage_rows": xwalk_rows,
            "exact_death_date_matches": source_date_matches,
            "death_type_concept_zero_rows": concept_zero_type_rows,
            "cause_concept_zero_rows": cause_zero_rows,
            "source_profile": source_profile,
            "death_type_policy": (
                "DEATH_SOURCE='L' is retained in lineage but not assigned a more specific "
                "OMOP Death Type concept without validated provenance semantics; "
                "death_type_concept_id=0."
            ),
            "death_date_policy": (
                "All source DEATH_DATE values are complete and DEATH_DATE_IMPUTE='N'; "
                "preserve the source date exactly."
            ),
            "cause_policy": (
                "PCORnet_DEATH_CAUSE is empty, so cause_concept_id=0, "
                "cause_source_concept_id=0, and cause_source_value=NULL."
            ),
            "status": status,
        }

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {**payload, "audit_path": str(audit_path)}
    finally:
        engine.dispose()
