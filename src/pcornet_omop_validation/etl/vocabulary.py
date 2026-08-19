from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists


# Athena files that map directly to OMOP CDM vocabulary tables. The order is
# dependency-friendly but constraints are not required for the initial load.
VOCABULARY_TABLES: tuple[tuple[str, str], ...] = (
    ("VOCABULARY.csv", "vocabulary"),
    ("DOMAIN.csv", "domain"),
    ("CONCEPT_CLASS.csv", "concept_class"),
    ("RELATIONSHIP.csv", "relationship"),
    ("CONCEPT.csv", "concept"),
    ("CONCEPT_RELATIONSHIP.csv", "concept_relationship"),
    ("CONCEPT_ANCESTOR.csv", "concept_ancestor"),
    ("CONCEPT_SYNONYM.csv", "concept_synonym"),
    ("DRUG_STRENGTH.csv", "drug_strength"),
)


@dataclass(frozen=True)
class VocabularyTableResult:
    file: str
    table: str
    source_rows: int
    target_rows: int
    sha256: str
    status: str


@dataclass(frozen=True)
class VocabularyLoadResult:
    database: str
    schema: str
    tables: list[VocabularyTableResult]
    audit_path: Path


def _validate_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQL {label}: {value!r}")
    return value


def _count_rows_and_hash(path: Path) -> tuple[int, str]:
    """Count data rows and hash a vocabulary file in one streaming pass."""
    digest = hashlib.sha256()
    newline_count = 0
    last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
            newline_count += chunk.count(b"\n")
            last_byte = chunk[-1:]

    # Athena files contain one header row. Account for a final row without LF.
    physical_rows = newline_count + (1 if last_byte and last_byte != b"\n" else 0)
    return max(physical_rows - 1, 0), digest.hexdigest()


def _bulk_insert_sql(schema: str, table: str, path: Path) -> str:
    """Build a Linux-compatible BULK INSERT for Athena tab-delimited files.

    Athena vocabulary downloads use a .csv suffix but are tab-delimited text files.
    They are not RFC CSV files and concept names may legitimately contain quotation
    marks, so FORMAT='CSV'/FIELDQUOTE must not be used. SQL Server on Linux rejects
    CODEPAGE in this execution path, so the server handles character conversion.
    """
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    escaped_path = str(path.resolve()).replace("'", "''")
    return f"""
BULK INSERT [{safe_schema}].[{safe_table}]
FROM '{escaped_path}'
WITH (
    FIRSTROW = 2,
    DATAFILETYPE = 'char',
    FIELDTERMINATOR = '0x09',
    ROWTERMINATOR = '0x0a',
    TABLOCK,
    KEEPNULLS
)
""".strip()


def _load_concept_via_staging(connection, schema: str, path: Path) -> None:
    """Bulk-load CONCEPT through a widened staging column.

    SQL Server 2022 on Linux can reject an Athena concept_name whose physical input
    length is exactly the OMOP varchar(255) boundary during BULK INSERT, even though
    the same value is accepted by a normal INSERT into dbo.concept. To keep Athena
    values unchanged, bulk-load into a schema-compatible transient table with only
    concept_name widened, then INSERT into the official OMOP table.
    """
    safe_schema = _validate_identifier(schema, "schema")
    stage = "etl_concept_vocabulary_stage"

    connection.exec_driver_sql(
        f"""
IF OBJECT_ID('[{safe_schema}].[{stage}]', 'U') IS NOT NULL
    DROP TABLE [{safe_schema}].[{stage}];

SELECT TOP (0) *
INTO [{safe_schema}].[{stage}]
FROM [{safe_schema}].[concept];

ALTER TABLE [{safe_schema}].[{stage}]
ALTER COLUMN [concept_name] varchar(4000) NOT NULL;
"""
    )
    connection.exec_driver_sql(_bulk_insert_sql(safe_schema, stage, path))

    stage_rows = int(
        connection.execute(
            text(f"SELECT COUNT_BIG(*) FROM [{safe_schema}].[{stage}]")
        ).scalar_one()
    )

    # Validate the one intentionally widened column before insertion into OMOP.
    overlength = int(
        connection.execute(
            text(
                f"SELECT COUNT_BIG(*) FROM [{safe_schema}].[{stage}] "
                "WHERE LEN([concept_name]) > 255"
            )
        ).scalar_one()
    )
    if overlength:
        raise RuntimeError(
            f"CONCEPT staging contains {overlength:,} concept_name value(s) longer than 255 characters"
        )

    connection.exec_driver_sql(
        f"""
INSERT INTO [{safe_schema}].[concept] (
    concept_id,
    concept_name,
    domain_id,
    vocabulary_id,
    concept_class_id,
    standard_concept,
    concept_code,
    valid_start_date,
    valid_end_date,
    invalid_reason
)
SELECT
    concept_id,
    concept_name,
    domain_id,
    vocabulary_id,
    concept_class_id,
    standard_concept,
    concept_code,
    valid_start_date,
    valid_end_date,
    invalid_reason
FROM [{safe_schema}].[{stage}];
"""
    )

    target_rows = int(
        connection.execute(
            text(f"SELECT COUNT_BIG(*) FROM [{safe_schema}].[concept]")
        ).scalar_one()
    )
    if target_rows != stage_rows:
        raise RuntimeError(
            "CONCEPT staging-to-target reconciliation failed: "
            f"stage={stage_rows:,}, target={target_rows:,}"
        )

    connection.exec_driver_sql(f"DROP TABLE [{safe_schema}].[{stage}]")


def load_vocabulary(config: EtlConfig) -> VocabularyLoadResult:
    """Load local Athena vocabulary files into the isolated OMOP target.

    The loader is non-destructive and resumable. Each table is committed only after
    source/target row-count reconciliation. On a later rerun, an already populated
    table is skipped only when its target row count exactly matches the source file;
    any other non-empty state fails rather than appending or replacing data.
    """
    vocab_dir = config.vocabulary_dir
    if not vocab_dir.is_dir():
        raise ValueError(f"Vocabulary directory does not exist: {vocab_dir}")

    sql_cfg = config.raw["sqlserver"]
    schema = _validate_identifier(str(sql_cfg.get("target_schema", "dbo")), "schema")
    database = str(sql_cfg["database"])

    available = [
        (filename, table)
        for filename, table in VOCABULARY_TABLES
        if (vocab_dir / filename).exists()
    ]
    if not available:
        raise ValueError(f"No recognized Athena vocabulary files found in {vocab_dir}")

    results: list[VocabularyTableResult] = []
    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            for filename, table in available:
                path = vocab_dir / filename
                if not table_exists(connection, schema, table):
                    raise RuntimeError(
                        f"Target table [{schema}].[{table}] does not exist. Run the schema stage first."
                    )

                source_rows, sha256 = _count_rows_and_hash(path)
                existing = int(
                    connection.execute(text(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]"))
                    .scalar_one()
                )

                if existing:
                    if existing == source_rows:
                        result = VocabularyTableResult(
                            file=str(path),
                            table=f"{schema}.{table}",
                            source_rows=source_rows,
                            target_rows=existing,
                            sha256=sha256,
                            status="already_loaded_matched",
                        )
                        results.append(result)
                        print(
                            f"Skipping {filename} -> {schema}.{table}: "
                            f"already loaded with {existing:,} rows [matched]",
                            flush=True,
                        )
                        continue
                    raise RuntimeError(
                        f"Target vocabulary table [{schema}].[{table}] already contains "
                        f"{existing:,} rows but source has {source_rows:,}; refusing to append or replace."
                    )

                print(
                    f"Loading {filename} -> {schema}.{table} ({source_rows:,} source rows)...",
                    flush=True,
                )
                try:
                    if table == "concept":
                        _load_concept_via_staging(connection, schema, path)
                    else:
                        connection.exec_driver_sql(_bulk_insert_sql(schema, table, path))
                    target_rows = int(
                        connection.execute(text(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]"))
                        .scalar_one()
                    )
                    status = "matched" if target_rows == source_rows else "row_count_mismatch"
                    if target_rows != source_rows:
                        connection.rollback()
                        raise RuntimeError(
                            f"Vocabulary reconciliation failed for {filename}: "
                            f"source={source_rows:,}, target={target_rows:,}"
                        )
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    if isinstance(exc, RuntimeError) and (
                        str(exc).startswith("Vocabulary reconciliation failed")
                        or str(exc).startswith("CONCEPT staging")
                    ):
                        raise
                    raise RuntimeError(
                        f"BULK INSERT failed for {path} -> [{schema}].[{table}]. "
                        "Confirm the SQL Server service account can read the vocabulary directory "
                        "and that the Athena file matches OMOP CDM 5.4 column order. "
                        f"Original error: {exc}"
                    ) from exc

                results.append(
                    VocabularyTableResult(
                        file=str(path),
                        table=f"{schema}.{table}",
                        source_rows=source_rows,
                        target_rows=target_rows,
                        sha256=sha256,
                        status=status,
                    )
                )
                print(f"  target rows: {target_rows:,} [{status}]", flush=True)
    finally:
        engine.dispose()

    audit_path = config.audit_dir / "vocabulary_load.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "database": database,
                "schema": schema,
                "vocabulary_directory": str(vocab_dir),
                "bulk_load_mode": "sql_server_linux_tab_delimited_concept_staged",
                "tables": [asdict(item) for item in results],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return VocabularyLoadResult(
        database=database,
        schema=schema,
        tables=results,
        audit_path=audit_path,
    )
