from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text

from .config import EtlConfig
from .database import make_engine, table_exists
from .preflight import OPTIONAL_SOURCE_TABLES, REQUIRED_SOURCE_TABLES


@dataclass(frozen=True)
class StagingTableResult:
    file: str
    table: str
    source_rows: int
    target_rows: int
    sha256: str
    status: str
    all_null_columns: list[str]


@dataclass(frozen=True)
class StagingLoadResult:
    database: str
    schema: str
    tables: list[StagingTableResult]
    missing_optional: list[str]
    audit_path: Path


def _validate_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQL {label}: {value!r}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_arrow_type(data_type: pa.DataType) -> pa.DataType:
    if pa.types.is_dictionary(data_type):
        return data_type.value_type
    return data_type


def _sql_type(data_type: pa.DataType) -> str:
    data_type = _normalize_arrow_type(data_type)

    # A Parquet column can have Arrow's null type when every value is null. The
    # physical file then contains no information from which to recover the intended
    # source datatype. For the staging layer, store such a column as nullable text.
    # This preserves the observed data exactly (all NULL) without guessing a semantic
    # datatype. The column is explicitly recorded in the staging audit so downstream
    # transforms can apply a domain-specific cast if one is required.
    if pa.types.is_null(data_type):
        return "nvarchar(max)"
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return "nvarchar(max)"
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        return "varbinary(max)"
    if pa.types.is_boolean(data_type):
        return "bit"
    if pa.types.is_int8(data_type) or pa.types.is_int16(data_type) or pa.types.is_int32(data_type):
        return "int"
    if pa.types.is_int64(data_type):
        return "bigint"
    if pa.types.is_uint8(data_type) or pa.types.is_uint16(data_type) or pa.types.is_uint32(data_type):
        return "bigint"
    if pa.types.is_uint64(data_type):
        return "decimal(20,0)"
    if pa.types.is_float32(data_type):
        return "real"
    if pa.types.is_float64(data_type):
        return "float"
    if pa.types.is_decimal(data_type):
        precision = min(int(data_type.precision), 38)
        scale = min(int(data_type.scale), precision)
        return f"decimal({precision},{scale})"
    if pa.types.is_date(data_type):
        return "date"
    if pa.types.is_timestamp(data_type):
        return "datetime2(7)"
    if pa.types.is_time(data_type):
        return "time(7)"

    raise TypeError(f"Unsupported Parquet/Arrow type for staging: {data_type}")


def _all_null_columns(arrow_schema: pa.Schema) -> list[str]:
    return [
        field.name
        for field in arrow_schema
        if pa.types.is_null(_normalize_arrow_type(field.type))
    ]


def _create_table_sql(schema: str, table: str, arrow_schema: pa.Schema) -> str:
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    columns: list[str] = []
    for field in arrow_schema:
        column = _validate_identifier(field.name, "column")
        columns.append(f"[{column}] {_sql_type(field.type)} NULL")
    return f"CREATE TABLE [{safe_schema}].[{safe_table}] (\n  " + ",\n  ".join(columns) + "\n)"


def _python_rows(batch: pa.RecordBatch) -> list[tuple[object, ...]]:
    # Arrow converts nulls to None and preserves date/datetime/decimal Python values.
    columns = [column.to_pylist() for column in batch.columns]
    return list(zip(*columns))


def _insert_sql(schema: str, table: str, column_names: list[str]) -> str:
    safe_schema = _validate_identifier(schema, "schema")
    safe_table = _validate_identifier(table, "table")
    safe_columns = [_validate_identifier(name, "column") for name in column_names]
    cols = ", ".join(f"[{name}]" for name in safe_columns)
    params = ", ".join("?" for _ in safe_columns)
    return f"INSERT INTO [{safe_schema}].[{safe_table}] ({cols}) VALUES ({params})"


def _target_count(connection, schema: str, table: str) -> int:
    return int(
        connection.execute(text(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]"))
        .scalar_one()
    )


def _load_one_table(
    config: EtlConfig,
    path: Path,
    schema: str,
    batch_size: int,
) -> StagingTableResult:
    stem = path.stem
    if stem.lower().startswith("pcornet_"):
        table_name = stem
    else:
        table_name = "PCORnet_" + stem.upper()
    table = _validate_identifier(table_name, "table")
    parquet = pq.ParquetFile(path)
    source_rows = int(parquet.metadata.num_rows)
    file_hash = _sha256(path)
    arrow_schema = parquet.schema_arrow
    all_null_columns = _all_null_columns(arrow_schema)

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            if table_exists(connection, schema, table):
                existing = _target_count(connection, schema, table)
                if existing == source_rows:
                    return StagingTableResult(
                        file=str(path),
                        table=f"{schema}.{table}",
                        source_rows=source_rows,
                        target_rows=existing,
                        sha256=file_hash,
                        status="already_loaded_matched",
                        all_null_columns=all_null_columns,
                    )
                if existing:
                    raise RuntimeError(
                        f"Existing staging table [{schema}].[{table}] has {existing:,} rows but "
                        f"source parquet has {source_rows:,}; refusing to append or overwrite."
                    )
                connection.exec_driver_sql(f"DROP TABLE [{schema}].[{table}]")
                connection.commit()

            connection.exec_driver_sql(_create_table_sql(schema, table, arrow_schema))
            connection.commit()
    finally:
        engine.dispose()

    # Use pyodbc fast_executemany in bounded Arrow record batches. This is slower than
    # server-side Parquet access but works reproducibly on local SQL Server/Linux and
    # preserves nulls and typed values without a lossy CSV intermediary.
    engine = make_engine(config)
    try:
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.fast_executemany = True
            sql = _insert_sql(schema, table, arrow_schema.names)
            loaded = 0
            for batch in parquet.iter_batches(batch_size=batch_size):
                rows = _python_rows(batch)
                if rows:
                    cursor.executemany(sql, rows)
                    raw.commit()
                    loaded += len(rows)
                    print(
                        f"  {table}: {loaded:,}/{source_rows:,} rows staged",
                        flush=True,
                    )
            cursor.close()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
    finally:
        engine.dispose()

    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            target_rows = _target_count(connection, schema, table)
    finally:
        engine.dispose()

    if target_rows != source_rows:
        raise RuntimeError(
            f"Staging reconciliation failed for {path.name}: "
            f"source={source_rows:,}, target={target_rows:,}"
        )

    return StagingTableResult(
        file=str(path),
        table=f"{schema}.{table}",
        source_rows=source_rows,
        target_rows=target_rows,
        sha256=file_hash,
        status="matched",
        all_null_columns=all_null_columns,
    )


def load_pcornet_staging(config: EtlConfig) -> StagingLoadResult:
    source_dir = config.source_dir
    if not source_dir.is_dir():
        raise ValueError(f"PCORnet parquet directory does not exist: {source_dir}")

    sql_cfg = config.raw["sqlserver"]
    schema = _validate_identifier(str(sql_cfg.get("source_schema", "dbo")), "schema")
    database = str(sql_cfg["database"])
    batch_size = int(config.raw.get("staging", {}).get("batch_size", 10_000))
    if batch_size < 1:
        raise ValueError("staging.batch_size must be a positive integer")

    required = sorted(REQUIRED_SOURCE_TABLES)
    missing_required = [name for name in required if not (source_dir / name).exists()]
    if missing_required:
        raise RuntimeError("Missing required PCORnet parquet files: " + ", ".join(missing_required))

    optional = sorted(OPTIONAL_SOURCE_TABLES)
    missing_optional = [name for name in optional if not (source_dir / name).exists()]

    input_files = [source_dir / name for name in required]
    input_files.extend(source_dir / name for name in optional if (source_dir / name).exists())

    results: list[StagingTableResult] = []
    for path in input_files:
        parquet = pq.ParquetFile(path)
        source_rows = int(parquet.metadata.num_rows)
        null_columns = _all_null_columns(parquet.schema_arrow)
        print(f"Staging {path.name} ({source_rows:,} rows)...", flush=True)
        if null_columns:
            print(
                "  all-null source column(s) stored as nullable nvarchar(max): "
                + ", ".join(null_columns),
                flush=True,
            )
        result = _load_one_table(config, path, schema, batch_size)
        results.append(result)
        if result.status == "already_loaded_matched":
            print(f"  skipped: already staged with {result.target_rows:,} rows [matched]", flush=True)
        else:
            print(f"  target rows: {result.target_rows:,} [matched]", flush=True)

    audit_path = config.audit_dir / "staging_load.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "database": database,
                "schema": schema,
                "source_directory": str(source_dir),
                "loader": "pyarrow_record_batches_pyodbc_fast_executemany",
                "batch_size": batch_size,
                "all_null_column_policy": "nullable_nvarchar_max_no_semantic_type_inference",
                "missing_optional": missing_optional,
                "tables": [asdict(item) for item in results],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return StagingLoadResult(
        database=database,
        schema=schema,
        tables=results,
        missing_optional=missing_optional,
        audit_path=audit_path,
    )
