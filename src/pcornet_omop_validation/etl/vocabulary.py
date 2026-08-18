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
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    escaped_path = str(path.resolve()).replace("'", "''")
    return f"""
BULK INSERT [{safe_schema}].[{safe_table}]
FROM '{escaped_path}'
WITH (
    FORMAT = 'CSV',
    FIRSTROW = 2,
    FIELDQUOTE = '"',
    FIELDTERMINATOR = '0x09',
    ROWTERMINATOR = '0x0a',
    CODEPAGE = '65001',
    TABLOCK,
    KEEPNULLS
)
""".strip()


def load_vocabulary(config: EtlConfig) -> VocabularyLoadResult:
    """Load local Athena vocabulary files into the isolated OMOP target.

    The loader is deliberately non-destructive. Any non-empty target vocabulary
    table causes a failure when fail_on_existing_target_rows=true. Each table is
    committed independently after source/target row-count reconciliation so a
    multi-gigabyte vocabulary load is not held in one transaction.
    """
    vocab_dir = config.vocabulary_dir
    if not vocab_dir.is_dir():
        raise ValueError(f"Vocabulary directory does not exist: {vocab_dir}")

    sql_cfg = config.raw["sqlserver"]
    schema = _validate_identifier(str(sql_cfg.get("target_schema", "dbo")), "schema")
    database = str(sql_cfg["database"])
    fail_existing = bool(config.raw["etl"].get("fail_on_existing_target_rows", True))

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

                existing = int(
                    connection.execute(text(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]"))
                    .scalar_one()
                )
                if existing:
                    if fail_existing:
                        raise RuntimeError(
                            f"Target vocabulary table [{schema}].[{table}] already contains {existing:,} rows; "
                            "refusing to append."
                        )
                    raise RuntimeError(
                        f"Target vocabulary table [{schema}].[{table}] is non-empty. "
                        "Automatic destructive replacement is not implemented."
                    )

                source_rows, sha256 = _count_rows_and_hash(path)
                print(
                    f"Loading {filename} -> {schema}.{table} ({source_rows:,} source rows)...",
                    flush=True,
                )
                try:
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
                    if isinstance(exc, RuntimeError) and str(exc).startswith("Vocabulary reconciliation failed"):
                        raise
                    raise RuntimeError(
                        f"BULK INSERT failed for {path} -> [{schema}].[{table}]. "
                        "Confirm the SQL Server service account can read the vocabulary directory "
                        f"and that the Athena file matches OMOP CDM 5.4 column order. Original error: {exc}"
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
