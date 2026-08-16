from __future__ import annotations

import json
import math
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from .mapping import DATE_HINTS, DOMAIN_MAP, KEY_HINTS, PATIENT_ID_CANDIDATES, SOURCE_PATIENT_ID_CANDIDATES


@dataclass(frozen=True)
class ProfileConfig:
    pcornet_dir: Path
    omop_dir: Path
    output_dir: Path
    minimum_cell_size: int = 11
    top_n_categories: int = 25


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parquet_expr(path: Path) -> str:
    return f"read_parquet({sql_string(str(path))})"


def list_parquet(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.parquet") if p.is_file())


def table_name(path: Path) -> str:
    return path.stem


def schema_df(con: duckdb.DuckDBPyConnection, path: Path) -> pd.DataFrame:
    return con.execute(f"DESCRIBE SELECT * FROM {parquet_expr(path)}").df()


def choose_column(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    lookup = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def looks_like_date(column: str, dtype: str) -> bool:
    text = f"{column} {dtype}".lower()
    return any(hint in column.lower() for hint in DATE_HINTS) or "date" in text or "timestamp" in text


def looks_like_key(column: str) -> bool:
    c = column.lower()
    return c.endswith("_id") or c in KEY_HINTS or any(c.endswith(h) for h in KEY_HINTS)


def safe_scalar(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def profile_table(con: duckdb.DuckDBPyConnection, path: Path, model: str):
    rel = parquet_expr(path)
    schema = schema_df(con, path)
    columns = schema["column_name"].tolist()
    n_rows = int(con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0])
    n_cols = len(columns)

    patient_col = choose_column(columns, PATIENT_ID_CANDIDATES[model])
    n_patients = None
    records_per_patient = pd.DataFrame()
    if patient_col:
        n_patients = int(con.execute(
            f"SELECT count(DISTINCT {qident(patient_col)}) FROM {rel} WHERE {qident(patient_col)} IS NOT NULL"
        ).fetchone()[0])
        rpp = con.execute(f"""
            SELECT count(*) AS records_per_patient
            FROM {rel}
            WHERE {qident(patient_col)} IS NOT NULL
            GROUP BY {qident(patient_col)}
        """).df()
        if not rpp.empty:
            qs = rpp["records_per_patient"].quantile([0, .25, .5, .75, .9, .95, .99, 1]).reset_index()
            qs.columns = ["quantile", "records_per_patient"]
            records_per_patient = qs

    null_rows = []
    date_rows = []
    key_rows = []
    for _, row in schema.iterrows():
        col = row["column_name"]
        dtype = row["column_type"]
        null_count = int(con.execute(f"SELECT count(*) FROM {rel} WHERE {qident(col)} IS NULL").fetchone()[0])
        distinct_count = int(con.execute(f"SELECT count(DISTINCT {qident(col)}) FROM {rel}").fetchone()[0])
        null_rows.append({
            "model": model,
            "table": table_name(path),
            "column": col,
            "type": dtype,
            "row_count": n_rows,
            "null_count": null_count,
            "null_rate": (null_count / n_rows) if n_rows else None,
            "distinct_count": distinct_count,
        })
        if looks_like_key(col):
            nonnull = n_rows - null_count
            key_rows.append({
                "model": model,
                "table": table_name(path),
                "column": col,
                "non_null_count": nonnull,
                "distinct_count": distinct_count,
                "duplicate_count": max(nonnull - distinct_count, 0),
                "unique_among_non_null": bool(nonnull == distinct_count),
            })
        if looks_like_date(col, dtype):
            try:
                mn, mx = con.execute(f"SELECT min({qident(col)}), max({qident(col)}) FROM {rel}").fetchone()
                date_rows.append({
                    "model": model,
                    "table": table_name(path),
                    "column": col,
                    "min": safe_scalar(mn),
                    "max": safe_scalar(mx),
                })
            except Exception as exc:
                date_rows.append({
                    "model": model,
                    "table": table_name(path),
                    "column": col,
                    "min": None,
                    "max": None,
                    "note": f"Could not summarize as date/time: {type(exc).__name__}",
                })

    exact_dupes = None
    if n_rows and n_cols <= 100:
        try:
            group_cols = ", ".join(qident(c) for c in columns)
            distinct_rows = int(con.execute(f"SELECT count(*) FROM (SELECT DISTINCT {group_cols} FROM {rel})").fetchone()[0])
            exact_dupes = n_rows - distinct_rows
        except Exception:
            exact_dupes = None

    summary = {
        "model": model,
        "table": table_name(path),
        "path": str(path),
        "row_count": n_rows,
        "column_count": n_cols,
        "patient_id_column": patient_col,
        "distinct_patients": n_patients,
        "exact_duplicate_rows": exact_dupes,
        "file_size_bytes": path.stat().st_size,
    }
    return summary, pd.DataFrame(null_rows), pd.DataFrame(date_rows), pd.DataFrame(key_rows), records_per_patient


def category_profiles(con: duckdb.DuckDBPyConnection, path: Path, model: str, minimum_cell_size: int, top_n: int) -> pd.DataFrame:
    rel = parquet_expr(path)
    schema = schema_df(con, path)
    n_rows = int(con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0])
    rows = []
    for _, sr in schema.iterrows():
        col = sr["column_name"]
        dtype = sr["column_type"].upper()
        if not any(t in dtype for t in ("CHAR", "VARCHAR", "STRING", "BOOLEAN")):
            continue
        distinct = int(con.execute(f"SELECT count(DISTINCT {qident(col)}) FROM {rel}").fetchone()[0])
        if distinct == 0 or distinct > 200:
            continue
        values = con.execute(f"""
            SELECT cast({qident(col)} AS VARCHAR) AS value, count(*) AS n
            FROM {rel}
            WHERE {qident(col)} IS NOT NULL
            GROUP BY 1
            ORDER BY n DESC, value
            LIMIT {int(top_n)}
        """).df()
        for _, r in values.iterrows():
            n = int(r["n"])
            rows.append({
                "model": model,
                "table": table_name(path),
                "column": col,
                "value": r["value"] if n >= minimum_cell_size else "<SUPPRESSED>",
                "n": n if n >= minimum_cell_size else None,
                "proportion": (n / n_rows) if (n_rows and n >= minimum_cell_size) else None,
                "suppressed": n < minimum_cell_size,
            })
    return pd.DataFrame(rows)


def person_crosswalk(con: duckdb.DuckDBPyConnection, pcornet_dir: Path, omop_dir: Path) -> pd.DataFrame:
    src = pcornet_dir / "PCORnet_DEMOGRAPHIC.parquet"
    dst = omop_dir / "person.parquet"
    if not src.exists() or not dst.exists():
        return pd.DataFrame()
    s_cols = schema_df(con, src)["column_name"].tolist()
    d_cols = schema_df(con, dst)["column_name"].tolist()
    patid = choose_column(s_cols, ["PATID"])
    source_value = choose_column(d_cols, SOURCE_PATIENT_ID_CANDIDATES)
    person_id = choose_column(d_cols, ["person_id"])
    if not (patid and source_value and person_id):
        return pd.DataFrame()
    srel, drel = parquet_expr(src), parquet_expr(dst)
    query = f"""
        WITH s AS (
            SELECT DISTINCT cast({qident(patid)} AS VARCHAR) AS source_id FROM {srel} WHERE {qident(patid)} IS NOT NULL
        ), d AS (
            SELECT cast({qident(source_value)} AS VARCHAR) AS source_id,
                   count(*) AS omop_rows,
                   count(DISTINCT {qident(person_id)}) AS omop_person_ids
            FROM {drel}
            WHERE {qident(source_value)} IS NOT NULL
            GROUP BY 1
        )
        SELECT
            (SELECT count(*) FROM s) AS pcornet_distinct_patids,
            (SELECT count(*) FROM d) AS omop_distinct_person_source_values,
            (SELECT count(*) FROM s JOIN d USING(source_id)) AS matched_source_ids,
            (SELECT count(*) FROM s LEFT JOIN d USING(source_id) WHERE d.source_id IS NULL) AS pcornet_patids_missing_in_omop,
            (SELECT count(*) FROM d LEFT JOIN s USING(source_id) WHERE s.source_id IS NULL) AS omop_source_ids_not_in_pcornet,
            (SELECT count(*) FROM d WHERE omop_person_ids > 1 OR omop_rows > 1) AS non_unique_omop_source_ids
    """
    return con.execute(query).df()


def concept_zero_profile(con: duckdb.DuckDBPyConnection, omop_dir: Path) -> pd.DataFrame:
    rows = []
    for path in list_parquet(omop_dir):
        cols = schema_df(con, path)["column_name"].tolist()
        n_rows = int(con.execute(f"SELECT count(*) FROM {parquet_expr(path)}").fetchone()[0])
        for col in cols:
            c = col.lower()
            if c.endswith("_concept_id") and not c.endswith("_source_concept_id"):
                zero = int(con.execute(f"SELECT count(*) FROM {parquet_expr(path)} WHERE {qident(col)} = 0").fetchone()[0])
                null = int(con.execute(f"SELECT count(*) FROM {parquet_expr(path)} WHERE {qident(col)} IS NULL").fetchone()[0])
                rows.append({
                    "table": table_name(path),
                    "column": col,
                    "row_count": n_rows,
                    "concept_id_0_count": zero,
                    "concept_id_0_rate": zero / n_rows if n_rows else None,
                    "null_count": null,
                })
    return pd.DataFrame(rows)


def source_concept_mapping_profile(con: duckdb.DuckDBPyConnection, omop_dir: Path) -> pd.DataFrame:
    rows = []
    for path in list_parquet(omop_dir):
        cols = schema_df(con, path)["column_name"].tolist()
        lower = {c.lower(): c for c in cols}
        for source_col_lower, source_col in lower.items():
            if not source_col_lower.endswith("_source_concept_id"):
                continue
            standard_col = lower.get(source_col_lower.replace("_source_concept_id", "_concept_id"))
            if not standard_col:
                continue
            n = int(con.execute(f"SELECT count(*) FROM {parquet_expr(path)}").fetchone()[0])
            result = con.execute(f"""
                SELECT
                    sum(CASE WHEN coalesce({qident(source_col)}, 0) <> 0 THEN 1 ELSE 0 END),
                    sum(CASE WHEN coalesce({qident(standard_col)}, 0) <> 0 THEN 1 ELSE 0 END),
                    sum(CASE WHEN coalesce({qident(source_col)}, 0) <> 0 AND coalesce({qident(standard_col)}, 0) = 0 THEN 1 ELSE 0 END)
                FROM {parquet_expr(path)}
            """).fetchone()
            rows.append({
                "table": table_name(path),
                "source_concept_column": source_col,
                "standard_concept_column": standard_col,
                "row_count": n,
                "source_concept_nonzero": int(result[0] or 0),
                "standard_concept_nonzero": int(result[1] or 0),
                "source_nonzero_standard_zero": int(result[2] or 0),
            })
    return pd.DataFrame(rows)


def write_df(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def run_profile(config: ProfileConfig) -> Path:
    out = config.output_dir
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    summaries, nulls, dates, keys = [], [], [], []

    for model, directory in (("pcornet", config.pcornet_dir), ("omop", config.omop_dir)):
        for path in list_parquet(directory):
            summary, ndf, ddf, kdf, rpp = profile_table(con, path, model)
            summaries.append(summary)
            nulls.append(ndf)
            dates.append(ddf)
            keys.append(kdf)
            if not rpp.empty:
                rpp.insert(0, "table", table_name(path))
                rpp.insert(0, "model", model)
                write_df(rpp, out / "table_profiles" / f"{model}__{table_name(path)}__records_per_patient.csv")
            cdf = category_profiles(con, path, model, config.minimum_cell_size, config.top_n_categories)
            if not cdf.empty:
                write_df(cdf, out / "categorical_profiles" / f"{model}__{table_name(path)}.csv")

    write_df(pd.DataFrame(summaries), out / "inventory.csv")
    write_df(pd.concat(nulls, ignore_index=True) if nulls else pd.DataFrame(), out / "column_profiles.csv")
    write_df(pd.concat(dates, ignore_index=True) if dates else pd.DataFrame(), out / "date_ranges.csv")
    write_df(pd.concat(keys, ignore_index=True) if keys else pd.DataFrame(), out / "key_profiles.csv")
    write_df(person_crosswalk(con, config.pcornet_dir, config.omop_dir), out / "person_crosswalk_summary.csv")
    write_df(concept_zero_profile(con, config.omop_dir), out / "omop_concept_zero_profile.csv")
    write_df(source_concept_mapping_profile(con, config.omop_dir), out / "omop_source_standard_mapping.csv")

    mapping_rows = [
        {"pcornet_table": src, "omop_table": dst}
        for src, targets in DOMAIN_MAP.items()
        for dst in targets
    ]
    write_df(pd.DataFrame(mapping_rows), out / "intended_domain_map.csv")

    metadata = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "duckdb": duckdb.__version__,
        "pcornet_dir": str(config.pcornet_dir),
        "omop_dir": str(config.omop_dir),
        "minimum_cell_size": config.minimum_cell_size,
        "top_n_categories": config.top_n_categories,
        "pcornet_files": [p.name for p in list_parquet(config.pcornet_dir)],
        "omop_files": [p.name for p in list_parquet(config.omop_dir)],
    }
    (out / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return out
