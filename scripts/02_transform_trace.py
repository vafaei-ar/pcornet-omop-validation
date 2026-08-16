from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

EXPECTED_SOURCE_FILES = {
    "DEMOGRAPHIC": "PCORnet_DEMOGRAPHIC.parquet",
    "DIAGNOSIS": "PCORnet_DIAGNOSIS.parquet",
    "ENCOUNTER": "PCORnet_ENCOUNTER.parquet",
    "ENROLLMENT": "PCORnet_ENROLLMENT.parquet",
    "LAB_RESULT_CM": "PCORnet_LAB_RESULT_CM.parquet",
    "DISPENSING": "PCORnet_DISPENSING.parquet",
    "MED_ADMIN": "PCORnet_MED_ADMIN.parquet",
    "PRESCRIBING": "PCORnet_PRESCRIBING.parquet",
    "PROCEDURES": "PCORnet_PROCEDURES.parquet",
    "OBS_GEN": "PCORnet_OBS_GEN.parquet",
    "OBS_CLIN": "PCORnet_OBS_CLIN.parquet",
    "VITAL": "PCORnet_VITAL.parquet",
    "PROVIDER": "PCORnet_PROVIDER.parquet",
}

# Hard-coded by the supplied PCORnet-to-OMOP ETL SQL.
DRUG_TYPE_SOURCE = {
    32825: "DISPENSING",
    32830: "MED_ADMIN",
    32838: "PRESCRIBING",
}


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def qs(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def rel(path: Path) -> str:
    return f"read_parquet({qs(str(path))})"


def columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    return con.execute(f"DESCRIBE SELECT * FROM {rel(path)}").df()["column_name"].tolist()


def write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def source_file_coverage(pcornet: Path) -> pd.DataFrame:
    present = {p.name for p in pcornet.glob("*.parquet")}
    return pd.DataFrame([
        {"source_domain": source, "expected_file": filename, "present": filename in present}
        for source, filename in EXPECTED_SOURCE_FILES.items()
    ])


def drug_source_trace(con: duckdb.DuckDBPyConnection, omop: Path) -> pd.DataFrame:
    path = omop / "drug_exposure.parquet"
    if not path.exists() or "drug_type_concept_id" not in columns(con, path):
        return pd.DataFrame()
    df = con.execute(f"""
        SELECT drug_type_concept_id, count(*) AS n,
               count(DISTINCT person_id) AS patients,
               sum(CASE WHEN drug_concept_id = 0 THEN 1 ELSE 0 END) AS drug_concept_id_0,
               sum(CASE WHEN visit_occurrence_id IS NULL THEN 1 ELSE 0 END) AS null_visit_occurrence_id,
               sum(CASE WHEN drug_exposure_start_date = DATE '1900-01-01' THEN 1 ELSE 0 END) AS sentinel_start_date,
               sum(CASE WHEN drug_exposure_end_date = DATE '1900-01-01' THEN 1 ELSE 0 END) AS sentinel_end_date
        FROM {rel(path)}
        GROUP BY 1
        ORDER BY n DESC
    """).df()
    df.insert(1, "etl_source", df["drug_type_concept_id"].map(DRUG_TYPE_SOURCE).fillna("UNKNOWN"))
    total = df["n"].sum()
    df["proportion"] = df["n"] / total if total else None
    return df


def type_concept_counts(con: duckdb.DuckDBPyConnection, omop: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(omop.glob("*.parquet")):
        for col in columns(con, path):
            if not col.lower().endswith("_type_concept_id"):
                continue
            df = con.execute(f"""
                SELECT {qi(col)} AS concept_id, count(*) AS n
                FROM {rel(path)}
                GROUP BY 1
                ORDER BY n DESC
            """).df()
            total = df["n"].sum()
            for _, r in df.iterrows():
                rows.append({
                    "table": path.stem,
                    "column": col,
                    "concept_id": r["concept_id"],
                    "n": int(r["n"]),
                    "proportion": float(r["n"] / total) if total else None,
                })
    return pd.DataFrame(rows)


def sentinel_dates(con: duckdb.DuckDBPyConnection, omop: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(omop.glob("*.parquet")):
        schema = con.execute(f"DESCRIBE SELECT * FROM {rel(path)}").df()
        for _, sr in schema.iterrows():
            col = sr["column_name"]
            typ = str(sr["column_type"]).upper()
            if "DATE" not in typ and "TIMESTAMP" not in typ:
                continue
            n, nulls, sentinel = con.execute(f"""
                SELECT count(*),
                       sum(CASE WHEN {qi(col)} IS NULL THEN 1 ELSE 0 END),
                       sum(CASE WHEN cast({qi(col)} AS DATE) = DATE '1900-01-01' THEN 1 ELSE 0 END)
                FROM {rel(path)}
            """).fetchone()
            rows.append({
                "table": path.stem,
                "column": col,
                "row_count": int(n),
                "null_count": int(nulls or 0),
                "sentinel_1900_01_01_count": int(sentinel or 0),
                "sentinel_1900_01_01_rate": float((sentinel or 0) / n) if n else None,
            })
    return pd.DataFrame(rows)


def visit_linkage(con: duckdb.DuckDBPyConnection, omop: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(omop.glob("*.parquet")):
        cols = columns(con, path)
        if "visit_occurrence_id" not in cols or "person_id" not in cols:
            continue
        n, linked, patients = con.execute(f"""
            SELECT count(*),
                   sum(CASE WHEN visit_occurrence_id IS NOT NULL THEN 1 ELSE 0 END),
                   count(DISTINCT person_id)
            FROM {rel(path)}
        """).fetchone()
        rows.append({
            "table": path.stem,
            "row_count": int(n),
            "distinct_patients": int(patients or 0),
            "linked_to_visit": int(linked or 0),
            "visit_linkage_rate": float((linked or 0) / n) if n else None,
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace source-domain contributions and ETL defaults in OMOP.")
    parser.add_argument("--pcornet", type=Path, required=True)
    parser.add_argument("--omop", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/transform_trace"))
    args = parser.parse_args()

    if not args.pcornet.is_dir():
        parser.error(f"PCORnet directory not found: {args.pcornet}")
    if not args.omop.is_dir():
        parser.error(f"OMOP directory not found: {args.omop}")

    con = duckdb.connect(database=":memory:")
    write(source_file_coverage(args.pcornet), args.output / "pcornet_source_file_coverage.csv")
    write(drug_source_trace(con, args.omop), args.output / "drug_exposure_etl_source_trace.csv")
    write(type_concept_counts(con, args.omop), args.output / "omop_type_concept_counts.csv")
    write(sentinel_dates(con, args.omop), args.output / "omop_sentinel_date_counts.csv")
    write(visit_linkage(con, args.omop), args.output / "omop_visit_linkage.csv")
    print(f"Transformation trace written to {args.output}")


if __name__ == "__main__":
    main()
