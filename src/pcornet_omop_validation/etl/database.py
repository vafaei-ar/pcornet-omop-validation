from __future__ import annotations

import re
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import EtlConfig


@dataclass(frozen=True)
class DatabaseStatus:
    server: str
    database: str
    connected: bool
    server_version: str | None = None


def _connection_url(config: EtlConfig, database: str | None = None) -> str:
    sql = config.raw["sqlserver"]
    password = config.sql_password
    if not password:
        env_name = sql.get("password_env", "OMOP_SQL_PASSWORD")
        raise ValueError(f"SQL Server password is not set in environment variable {env_name}")

    db = database or str(sql["database"])
    trust = "yes" if sql.get("trust_server_certificate", True) else "no"
    conn_str = (
        f"DRIVER={{{sql.get('driver', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={sql['server']};DATABASE={db};UID={sql['user']};PWD={password};"
        f"TrustServerCertificate={trust};Encrypt=yes;"
    )
    return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(conn_str)


def make_engine(config: EtlConfig, database: str | None = None) -> Engine:
    return create_engine(_connection_url(config, database), future=True, pool_pre_ping=True)


@contextmanager
def connect(config: EtlConfig, database: str | None = None) -> Iterator[Connection]:
    engine = make_engine(config, database)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def check_connection(config: EtlConfig) -> DatabaseStatus:
    sql = config.raw["sqlserver"]
    with connect(config) as connection:
        version = connection.execute(
            text("SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(128))")
        ).scalar_one()
    return DatabaseStatus(
        server=str(sql["server"]),
        database=str(sql["database"]),
        connected=True,
        server_version=str(version),
    )


def database_exists(config: EtlConfig, database: str) -> bool:
    engine = make_engine(config, database="master")
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT 1 FROM sys.databases WHERE name = :database"),
                {"database": database},
            ).scalar()
        return result == 1
    finally:
        engine.dispose()


def ensure_database(config: EtlConfig) -> bool:
    """Create the configured target database if absent.

    Returns True when a new database was created, False when it already existed.
    SQL Server requires CREATE DATABASE outside a user transaction, so this uses
    AUTOCOMMIT explicitly.
    """
    database = str(config.raw["sqlserver"]["database"])
    if database_exists(config, database):
        return False

    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise ValueError("Database name may contain only letters, numbers, and underscores")

    engine = make_engine(config, database="master")
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f"CREATE DATABASE [{database}]")
    finally:
        engine.dispose()
    return True


def table_exists(connection: Connection, schema: str, table: str) -> bool:
    result = connection.execute(
        text(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
        ),
        {"schema": schema, "table": table},
    ).scalar()
    return result == 1


def split_sql_server_batches(sql_text: str) -> list[str]:
    """Split a SQL Server script on standalone GO batch separators."""
    parts = re.split(r"^\s*GO\s*(?:--.*)?$", sql_text, flags=re.IGNORECASE | re.MULTILINE)
    return [part.strip() for part in parts if part.strip()]


def execute_script(connection: Connection, sql_text: str) -> int:
    batches = split_sql_server_batches(sql_text)
    for batch in batches:
        connection.exec_driver_sql(batch)
    return len(batches)
