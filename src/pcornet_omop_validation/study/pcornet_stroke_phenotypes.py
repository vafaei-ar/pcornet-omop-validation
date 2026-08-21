from __future__ import annotations

"""PCORnet-only verification of the prespecified stroke phenotypes D0, D1, and D3.

This audit intentionally runs before any OMOP comparison. Its patient-selection
logic follows the PSU PROMIS ML extraction pipeline as closely as possible while
turning off registry gating/augmentation for the EHR-only reproducibility study:

* exact prespecified ischemic-stroke ICD code list
* EI/IP encounters
* primary diagnosis (PDX == P)
* at least one overnight stay (calendar-day difference >= 1)
* first qualifying admission per phenotype definition
* age >= 18 after index selection, matching the ML pipeline ordering

D1 adds CT-or-MRI evidence and a lipid LOINC during the prespecified windows.
D3 adds MRI evidence and a lipid LOINC during the same windows.

The script also reports a diagnostic count requiring recognized DX_TYPE values.
That restriction is NOT part of the ML pipeline's exact-code matcher and is not
used for the primary PCORnet phenotype counts. This makes it possible to detect
whether an over-restrictive DX_TYPE requirement explains a low cohort count.
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from .stroke_codes import ICD10_STROKE_CODES, ICD9_STROKE_CODES


CT_CPTS = frozenset({"70450", "70460", "70470"})
MRI_CPTS = frozenset({"70551", "70552", "70553", "70557", "70558", "70559"})
ALL_IMAGING_CPTS = CT_CPTS | MRI_CPTS
RECOGNIZED_DX_TYPES = frozenset({"09", "9", "ICD9", "ICD9CM", "10", "ICD10", "ICD10CM"})
# Raw PCORnet uses CH for CPT/HCPCS. Harmonized PROMIS files may use CPT/CPT4.
CPT_CODE_TYPES = frozenset({"CH", "CPT", "CPT4", "HCPCS"})


FILE_CANDIDATES = {
    "diagnosis": ("diagnosis.parquet", "DIAGNOSIS.parquet", "PCORnet_DIAGNOSIS.parquet"),
    "encounter": ("encounter.parquet", "ENCOUNTER.parquet", "PCORnet_ENCOUNTER.parquet"),
    "demographic": ("demographic.parquet", "DEMOGRAPHIC.parquet", "PCORnet_DEMOGRAPHIC.parquet"),
    "procedures": ("procedures.parquet", "PROCEDURES.parquet", "PCORnet_PROCEDURES.parquet"),
    "lab": ("lab_result_cm.parquet", "LAB_RESULT_CM.parquet", "PCORnet_LAB_RESULT_CM.parquet"),
}


def _sql_list(values: Iterable[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _norm(column: str) -> str:
    return f"replace(upper(trim(cast({column} as varchar))), '.', '')"


def _norm_text(column: str) -> str:
    return f"upper(trim(cast({column} as varchar)))"


def _resolve_file(root: Path, key: str, required: bool = True) -> Path | None:
    for name in FILE_CANDIDATES[key]:
        path = root / name
        if path.exists():
            return path
    lower_map = {p.name.lower(): p for p in root.glob("*.parquet")}
    for name in FILE_CANDIDATES[key]:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    if required:
        raise FileNotFoundError(f"Could not find {key} parquet under {root}")
    return None


def _columns(con: duckdb.DuckDBPyConnection, path: Path) -> set[str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({_literal(path)})").fetchall()
    return {str(row[0]).upper() for row in rows}


def _pick(columns: set[str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate.upper() in columns:
            return candidate.upper()
    return None


def _count(con: duckdb.DuckDBPyConnection, relation: str) -> dict[str, int]:
    rows, patients = con.execute(
        f"SELECT count(*)::BIGINT, count(DISTINCT PATID)::BIGINT FROM {relation}"
    ).fetchone()
    return {"rows": int(rows), "patients": int(patients)}


def _discover_lipid_csv(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        [
            Path.home() / "works/repos/PROMIS-ML-pipeline/external_data/lipid_corrected_by_harold.csv",
            Path.home() / "works/PROMIS-ML-pipeline/external_data/lipid_corrected_by_harold.csv",
            Path.home() / "PROMIS-ML-pipeline/external_data/lipid_corrected_by_harold.csv",
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "The exact lipid LOINC whitelist is required for D1/D3. Pass --lipid-loinc-csv "
        "pointing to PROMIS-ML-pipeline/external_data/lipid_corrected_by_harold.csv."
    )


def _load_lipid_loincs(path: Path) -> set[str]:
    df = pd.read_csv(path, dtype=str)
    column = next(
        (c for c in ["LOINC_NUM", "LOINC", "LOINC_CD", "LOINC_CODE", "ComponentCode"] if c in df.columns),
        df.columns[0],
    )
    values = {str(x).strip().upper() for x in df[column].dropna()}
    values.discard("")
    values.discard("NAN")
    if not values:
        raise RuntimeError(f"No lipid LOINCs found in {path}")
    return values


def _age_expression(birth: str, index_date: str) -> str:
    # The ML pipeline uses integer floor((index-birth).days / 365), not birthday-aware age.
    return f"floor(date_diff('day', cast({birth} as date), cast({index_date} as date)) / 365.0)"


def run_dataset(
    pcornet_dir: str,
    label: str,
    lipid_loinc_csv: str | None = None,
    output_dir: str = "results/study_planning/pcornet_verification",
) -> dict[str, object]:
    root = Path(pcornet_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)

    files = {
        "diagnosis": _resolve_file(root, "diagnosis"),
        "encounter": _resolve_file(root, "encounter"),
        "demographic": _resolve_file(root, "demographic"),
        "procedures": _resolve_file(root, "procedures"),
        "lab": _resolve_file(root, "lab"),
    }
    lipid_path = _discover_lipid_csv(lipid_loinc_csv)
    lipid_loincs = _load_lipid_loincs(lipid_path)

    con = duckdb.connect(database=":memory:")
    try:
        cols = {key: _columns(con, path) for key, path in files.items() if path is not None}
        required_dx = {"PATID", "ENCOUNTERID", "DX", "PDX"}
        required_enc = {"PATID", "ENCOUNTERID", "ENC_TYPE", "ADMIT_DATE", "DISCHARGE_DATE"}
        required_dm = {"PATID", "BIRTH_DATE"}
        for key, required in [("diagnosis", required_dx), ("encounter", required_enc), ("demographic", required_dm)]:
            missing = required - cols[key]
            if missing:
                raise RuntimeError(f"{files[key]} missing required columns: {sorted(missing)}")

        con.execute(f"CREATE VIEW dx AS SELECT * FROM read_parquet({_literal(files['diagnosis'])})")
        con.execute(f"CREATE VIEW enc AS SELECT * FROM read_parquet({_literal(files['encounter'])})")
        con.execute(f"CREATE VIEW demo AS SELECT * FROM read_parquet({_literal(files['demographic'])})")
        con.execute(f"CREATE VIEW proc AS SELECT * FROM read_parquet({_literal(files['procedures'])})")
        con.execute(f"CREATE VIEW lab AS SELECT * FROM read_parquet({_literal(files['lab'])})")

        all_stroke_codes = ICD9_STROKE_CODES | ICD10_STROKE_CODES
        dx_has_enc_type = "ENC_TYPE" in cols["diagnosis"]
        dx_has_date = "DX_DATE" in cols["diagnosis"]
        dx_has_type = "DX_TYPE" in cols["diagnosis"]

        dx_enc_filter = (
            f"{_norm_text('ENC_TYPE')} IN ({_sql_list({'EI', 'IP'})})"
            if dx_has_enc_type
            else "TRUE"
        )
        stroke_filter = f"{_norm('DX')} IN ({_sql_list(all_stroke_codes)})"
        recognized_type_filter = (
            f"{_norm_text('DX_TYPE')} IN ({_sql_list(RECOGNIZED_DX_TYPES)})"
            if dx_has_type
            else "FALSE"
        )
        dx_date_expr = "cast(DX_DATE as date)" if dx_has_date else "NULL::DATE"

        con.execute(
            f"""
            CREATE TEMP VIEW dx_eiip AS
            SELECT * FROM dx WHERE {dx_enc_filter};

            CREATE TEMP VIEW stroke_dx AS
            SELECT * FROM dx_eiip WHERE {stroke_filter};

            CREATE TEMP VIEW primary_stroke_dx AS
            SELECT * FROM stroke_dx WHERE {_norm_text('PDX')} = 'P';

            CREATE TEMP VIEW primary_stroke_dx_recognized_type AS
            SELECT * FROM primary_stroke_dx WHERE {recognized_type_filter};

            CREATE TEMP VIEW stroke_encounters AS
            SELECT DISTINCT
                cast(e.PATID as varchar) AS PATID,
                cast(e.ENCOUNTERID as varchar) AS ENCOUNTERID,
                cast(e.ADMIT_DATE as date) AS ADMIT_DATE,
                cast(e.DISCHARGE_DATE as date) AS DISCHARGE_DATE,
                {_norm_text('e.ENC_TYPE')} AS ENC_TYPE
            FROM enc e
            JOIN (SELECT DISTINCT cast(ENCOUNTERID as varchar) AS ENCOUNTERID FROM primary_stroke_dx) d
              ON cast(e.ENCOUNTERID as varchar) = d.ENCOUNTERID
            WHERE {_norm_text('e.ENC_TYPE')} IN ('EI','IP');

            CREATE TEMP VIEW overnight_stroke_encounters AS
            SELECT *, date_diff('day', ADMIT_DATE, DISCHARGE_DATE) AS enc_duration
            FROM stroke_encounters
            WHERE ADMIT_DATE IS NOT NULL
              AND DISCHARGE_DATE IS NOT NULL
              AND date_diff('day', ADMIT_DATE, DISCHARGE_DATE) >= 1;

            CREATE TEMP VIEW dx_one AS
            SELECT ENCOUNTERID, DX, DX_TYPE, DX_DATE
            FROM (
                SELECT
                    cast(ENCOUNTERID as varchar) AS ENCOUNTERID,
                    cast(DX as varchar) AS DX,
                    {('cast(DX_TYPE as varchar)' if dx_has_type else 'NULL::VARCHAR')} AS DX_TYPE,
                    {dx_date_expr} AS DX_DATE,
                    row_number() OVER (
                        PARTITION BY cast(ENCOUNTERID as varchar)
                        ORDER BY {dx_date_expr} NULLS LAST, {_norm('DX')}
                    ) AS rn
                FROM primary_stroke_dx
            ) q
            WHERE rn = 1;

            CREATE TEMP VIEW d0_encounters AS
            SELECT
                e.PATID,
                e.ENCOUNTERID,
                e.ADMIT_DATE,
                e.DISCHARGE_DATE,
                e.enc_duration,
                d.DX,
                d.DX_TYPE,
                d.DX_DATE,
                coalesce(d.DX_DATE, e.ADMIT_DATE, e.DISCHARGE_DATE) AS INDEX_DATE
            FROM overnight_stroke_encounters e
            LEFT JOIN dx_one d USING (ENCOUNTERID);
            """
        )

        proc_cols = cols["procedures"]
        proc_patid = _pick(proc_cols, ["PATID"])
        proc_code = _pick(proc_cols, ["PX", "PX_CD", "CPT", "PROC_CODE", "PROCEDURE_CODE"])
        proc_date = _pick(proc_cols, ["PX_DATE", "PX_DT", "PROC_DATE", "PROCEDURE_DATE"])
        proc_type = _pick(proc_cols, ["PX_TYPE", "PX_CD_TYPE", "CODE_TYPE", "RAW_PX_TYPE"])
        if not proc_patid or not proc_code or not proc_date:
            raise RuntimeError("PROCEDURES lacks PATID, procedure code, or procedure date required for D1/D3")
        proc_type_filter = (
            f"AND {_norm_text(proc_type)} IN ({_sql_list(CPT_CODE_TYPES)})" if proc_type else ""
        )

        lab_cols = cols["lab"]
        lab_patid = _pick(lab_cols, ["PATID"])
        lab_loinc = _pick(lab_cols, ["LAB_LOINC", "LAB_LOINC_CD", "LOINC", "LOINC_CODE"])
        lab_date = _pick(lab_cols, ["LAB_TKN_DTTM", "SPECIMEN_DATE", "LAB_DATE", "RESULT_DATE"])
        if not lab_patid or not lab_loinc or not lab_date:
            raise RuntimeError("LAB_RESULT_CM lacks PATID, LOINC, or lab date required for D1/D3")

        con.execute(
            f"""
            CREATE TEMP VIEW evidence AS
            SELECT
                e.*,
                CASE WHEN EXISTS (
                    SELECT 1 FROM proc p
                    WHERE cast(p.{proc_patid} as varchar) = e.PATID
                      AND {_norm('p.' + proc_code)} IN ({_sql_list(ALL_IMAGING_CPTS)})
                      {proc_type_filter.replace(proc_type or '', 'p.' + proc_type) if proc_type else ''}
                      AND cast(p.{proc_date} as date) BETWEEN e.ADMIT_DATE - INTERVAL 2 DAY AND e.DISCHARGE_DATE
                ) THEN 1 ELSE 0 END AS has_ct_or_mri,
                CASE WHEN EXISTS (
                    SELECT 1 FROM proc p
                    WHERE cast(p.{proc_patid} as varchar) = e.PATID
                      AND {_norm('p.' + proc_code)} IN ({_sql_list(MRI_CPTS)})
                      {proc_type_filter.replace(proc_type or '', 'p.' + proc_type) if proc_type else ''}
                      AND cast(p.{proc_date} as date) BETWEEN e.ADMIT_DATE - INTERVAL 2 DAY AND e.DISCHARGE_DATE
                ) THEN 1 ELSE 0 END AS has_mri,
                CASE WHEN EXISTS (
                    SELECT 1 FROM lab l
                    WHERE cast(l.{lab_patid} as varchar) = e.PATID
                      AND {_norm_text('l.' + lab_loinc)} IN ({_sql_list(lipid_loincs)})
                      AND cast(l.{lab_date} as date) BETWEEN e.ADMIT_DATE AND e.DISCHARGE_DATE
                ) THEN 1 ELSE 0 END AS has_lipid
            FROM d0_encounters e;

            CREATE TEMP VIEW d0_ranked AS
            SELECT *, row_number() OVER (PARTITION BY PATID ORDER BY INDEX_DATE, ENCOUNTERID) AS rn
            FROM evidence;

            CREATE TEMP VIEW d1_ranked AS
            SELECT *, row_number() OVER (PARTITION BY PATID ORDER BY INDEX_DATE, ENCOUNTERID) AS rn
            FROM evidence WHERE has_ct_or_mri = 1 AND has_lipid = 1;

            CREATE TEMP VIEW d3_ranked AS
            SELECT *, row_number() OVER (PARTITION BY PATID ORDER BY INDEX_DATE, ENCOUNTERID) AS rn
            FROM evidence WHERE has_mri = 1 AND has_lipid = 1;
            """
        )

        # Match the ML pipeline ordering: first qualifying encounter, then calculate/filter age.
        for phenotype in ("d0", "d1", "d3"):
            con.execute(
                f"""
                CREATE TEMP VIEW {phenotype}_first_pre_age AS
                SELECT * FROM {phenotype}_ranked WHERE rn = 1;

                CREATE TEMP VIEW {phenotype}_first AS
                SELECT f.*, {_age_expression('d.BIRTH_DATE', 'f.INDEX_DATE')} AS AGE_AT_STROKE
                FROM {phenotype}_first_pre_age f
                JOIN demo d ON cast(d.PATID as varchar) = f.PATID
                WHERE d.BIRTH_DATE IS NOT NULL
                  AND {_age_expression('d.BIRTH_DATE', 'f.INDEX_DATE')} >= 18;
                """
            )

        funnel = {
            "diagnosis_all": _count(con, "dx"),
            "diagnosis_after_ei_ip_filter": _count(con, "dx_eiip"),
            "exact_stroke_dx": _count(con, "stroke_dx"),
            "primary_exact_stroke_dx": _count(con, "primary_stroke_dx"),
            "primary_exact_stroke_dx_with_recognized_dx_type": _count(con, "primary_stroke_dx_recognized_type"),
            "matched_stroke_encounters": _count(con, "stroke_encounters"),
            "overnight_stroke_encounters": _count(con, "overnight_stroke_encounters"),
            "d0_first_pre_age": _count(con, "d0_first_pre_age"),
            "d0_final": _count(con, "d0_first"),
            "d1_eligible_encounters": _count(con, "d1_ranked"),
            "d1_first_pre_age": _count(con, "d1_first_pre_age"),
            "d1_final": _count(con, "d1_first"),
            "d3_eligible_encounters": _count(con, "d3_ranked"),
            "d3_first_pre_age": _count(con, "d3_first_pre_age"),
            "d3_final": _count(con, "d3_first"),
        }

        dx_type_distribution: list[dict[str, object]] = []
        if dx_has_type:
            for dx_type, n_rows, n_patients in con.execute(
                """
                SELECT coalesce(cast(DX_TYPE as varchar), '<NULL>') AS DX_TYPE,
                       count(*)::BIGINT, count(DISTINCT PATID)::BIGINT
                FROM primary_stroke_dx
                GROUP BY 1 ORDER BY 2 DESC
                """
            ).fetchall():
                dx_type_distribution.append(
                    {"dx_type": str(dx_type), "rows": int(n_rows), "patients": int(n_patients)}
                )

        phenotype_rows = con.execute(
            """
            WITH ids AS (
                SELECT PATID FROM d0_first
                UNION SELECT PATID FROM d1_first
                UNION SELECT PATID FROM d3_first
            )
            SELECT
                ids.PATID,
                CASE WHEN d0.PATID IS NOT NULL THEN 1 ELSE 0 END AS D0,
                d0.INDEX_DATE AS D0_INDEX_DATE,
                CASE WHEN d1.PATID IS NOT NULL THEN 1 ELSE 0 END AS D1,
                d1.INDEX_DATE AS D1_INDEX_DATE,
                CASE WHEN d3.PATID IS NOT NULL THEN 1 ELSE 0 END AS D3,
                d3.INDEX_DATE AS D3_INDEX_DATE
            FROM ids
            LEFT JOIN d0_first d0 USING (PATID)
            LEFT JOIN d1_first d1 USING (PATID)
            LEFT JOIN d3_first d3 USING (PATID)
            ORDER BY ids.PATID
            """
        ).fetchall()

        summary: dict[str, object] = {
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "pcornet_dir": str(root),
            "files": {k: str(v) for k, v in files.items()},
            "selection_reference": {
                "ml_pipeline": "TheDecodeLab/PROMIS-ML-pipeline stroke_extraction_scripts/02_extract_stroke_patients.py",
                "registry_augmentation": "disabled for this EHR-only reproducibility study",
                "stroke_code_matching": "exact prespecified list; DX_TYPE is not required by the ML exact-code matcher",
                "encounter_types": ["EI", "IP"],
                "primary_diagnosis": "PDX=P",
                "minimum_duration_days": 1,
                "age": ">=18 after first qualifying encounter selection, matching ML pipeline order",
                "D0": "base qualifying ischemic-stroke phenotype",
                "D1": "D0 encounter + CT or MRI from 2 days before admission through discharge + lipid LOINC during hospitalization",
                "D3": "D0 encounter + MRI from 2 days before admission through discharge + lipid LOINC during hospitalization",
            },
            "code_sets": {
                "icd9_n": len(ICD9_STROKE_CODES),
                "icd10_n": len(ICD10_STROKE_CODES),
                "ct_cpt_n": len(CT_CPTS),
                "mri_cpt_n": len(MRI_CPTS),
                "lipid_loinc_n": len(lipid_loincs),
                "lipid_loinc_source": str(lipid_path),
            },
            "schema_diagnostics": {
                "diagnosis_has_enc_type": dx_has_enc_type,
                "diagnosis_has_dx_type": dx_has_type,
                "diagnosis_has_dx_date": dx_has_date,
                "procedure_code_column": proc_code,
                "procedure_type_column": proc_type,
                "procedure_date_column": proc_date,
                "lab_loinc_column": lab_loinc,
                "lab_date_column": lab_date,
            },
            "funnel": funnel,
            "dx_type_distribution_among_primary_exact_stroke_dx": dx_type_distribution,
            "primary_counts": {
                "D0": funnel["d0_final"]["patients"],
                "D1": funnel["d1_final"]["patients"],
                "D3": funnel["d3_final"]["patients"],
            },
        }

        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        json_path = out / f"{safe_label}_pcornet_stroke_phenotypes.json"
        csv_path = out / f"{safe_label}_pcornet_stroke_phenotype_patients.csv"
        json_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["PATID", "D0", "D0_INDEX_DATE", "D1", "D1_INDEX_DATE", "D3", "D3_INDEX_DATE"])
            writer.writerows(phenotype_rows)
        summary["output_json"] = str(json_path)
        summary["output_csv"] = str(csv_path)
        return summary
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify PCORnet D0/D1/D3 stroke phenotypes before OMOP comparison")
    parser.add_argument("--pcornet-dir", required=True, help="Directory containing PCORnet parquet tables")
    parser.add_argument("--label", required=True, help="Short dataset label used in output filenames")
    parser.add_argument("--lipid-loinc-csv", default=None, help="Exact PROMIS lipid LOINC whitelist CSV")
    parser.add_argument("--output-dir", default="results/study_planning/pcornet_verification")
    args = parser.parse_args()
    result = run_dataset(args.pcornet_dir, args.label, args.lipid_loinc_csv, args.output_dir)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
