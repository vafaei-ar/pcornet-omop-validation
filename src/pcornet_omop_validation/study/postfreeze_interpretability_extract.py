from __future__ import annotations

"""Private, guarded four-cohort feature exporter for post-freeze interpretability.

Runs the unchanged locked Stage E and post-freeze Stage D fallback programs.
Uses narrowly scoped, fail-closed read interception to copy native features
while their existing SQL Server #temp tables are alive. Never commits or
publishes these row-level local files. No frozen ETL or study definitions change.
"""
import argparse
import json
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.study import (
    stage_e_statistical_model_reproducibility as original_stage_e,
    stage_e_statistical_model_reproducibility_anchorfix as stage_e_anchorfix,
    stage_d_encounter_date_fallback_sensitivity as stage_d_fallback,
)
from pcornet_omop_validation.study.postfreeze_model_interpretability import (
    EXPECTED, FEATURES, read_population, validate_populations,
)
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def capture_stage_e(config_path: str, working_output_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Capture only existing native feature queries; preserve original Stage E behavior."""
    original_read_sql = pd.read_sql
    captures: dict[str, pd.DataFrame] = {}

    def intercept(sql, con, *args, **kwargs):
        query = str(sql)
        key = None
        before = None
        after = None
        if "SELECT s.patid,s.outcome," in query and "FROM #src90 s" in query:
            key = "source"
            before = "SELECT s.patid,s.outcome,"
            after = "SELECT s.patid,s.index_date,s.outcome,"
        elif "SELECT o.patid,o.outcome," in query and "FROM #omop90 o" in query:
            key = "omop"
            before = "SELECT o.patid,o.outcome,"
            after = "SELECT o.patid,o.index_date,o.outcome,"
        if key is None:
            return original_read_sql(sql, con, *args, **kwargs)
        if key in captures or query.count(before) != 1:
            raise RuntimeError("Unrecognized or repeated Stage E feature-query capture")
        modified = query.replace(before, after, 1)
        frame = original_read_sql(text(modified), con, *args, **kwargs)
        if "index_date" not in frame:
            raise RuntimeError("Stage E capture did not return index_date")
        captures[key] = frame.copy()
        return frame  # Additional index_date is ignored by original Stage E feature/model logic.

    pd.read_sql = intercept
    try:
        # This invokes the approved anchor-fix wrapper, not the initial failed version.
        stage_e_anchorfix.run(config_path, working_output_dir)
    finally:
        pd.read_sql = original_read_sql
    if set(captures) != {"source", "omop"}:
        raise RuntimeError("Stage E feature queries did not both execute")
    return captures["source"], captures["omop"]


def capture_fallback(config_path: str, working_output_dir: str) -> pd.DataFrame:
    """Capture native OMOP features on the validated target-date fallback D0 overlay."""
    cfg = load_etl_config(config_path)
    schema = original_stage_e._schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    all_stroke = original_stage_e._sql_list(
        set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES)
    )
    acute_ids = ",".join(str(v) for v in original_stage_e.ACUTE_VISIT_CONCEPT_IDS)
    query = f"""
        SELECT t.patid,t.index_date,CAST(t.event90 AS int) outcome,
          CAST(FLOOR(DATEDIFF(day,CAST(p.birth_datetime AS date),t.index_date)/365.0) AS float) age_at_index_years,
          CASE WHEN p.gender_concept_id=8532 THEN 1.0 WHEN p.gender_concept_id=8507 THEN 0.0 ELSE NULL END female_indicator,
          CAST(DATEDIFF(day,t.target_admit_date,t.target_discharge_date) AS float) index_length_of_stay_days,
          CAST((SELECT COUNT_BIG(*) FROM [{schema}].[visit_occurrence] v
            WHERE v.person_id=t.person_id AND v.visit_start_date IS NOT NULL
              AND DATEDIFF(day,CAST(v.visit_start_date AS date),t.index_date) BETWEEN 1 AND 365
              AND v.visit_concept_id IN ({acute_ids})) AS float) prior_365d_acute_care_encounter_count,
          CAST((SELECT COUNT_BIG(*) FROM [{schema}].[visit_occurrence] v
            WHERE v.person_id=t.person_id AND v.visit_start_date IS NOT NULL
              AND DATEDIFF(day,CAST(v.visit_start_date AS date),t.index_date) BETWEEN 1 AND 365
          ) AS float) prior_365d_all_encounter_count,
          CASE WHEN EXISTS (
            SELECT 1 FROM [{schema}].[condition_occurrence] c
            WHERE c.person_id=t.person_id AND c.condition_start_date IS NOT NULL
              AND DATEDIFF(day,CAST(c.condition_start_date AS date),t.index_date) BETWEEN 1 AND 365
              AND {original_stage_e._norm('c.condition_source_value')} IN ({all_stroke})
          ) THEN 1.0 ELSE 0.0 END prior_365d_ischemic_stroke_indicator
        FROM #fd_tgt_labels t
        JOIN [{schema}].[person] p ON p.person_id=t.person_id
        WHERE t.covered90=1
    """
    original_exec = Connection.exec_driver_sql
    original_read_sql = pd.read_sql
    captures: list[pd.DataFrame] = []

    def intercept(self, statement, *args, **kwargs):
        marker = "INTO #fd_tgt_labels"
        # Only one exact validated Stage D query creates the target labels.
        if marker not in statement:
            return original_exec(self, statement, *args, **kwargs)
        if captures:
            raise RuntimeError("Repeated fallback feature-query capture")
        result = original_exec(self, statement, *args, **kwargs)
        frame = original_read_sql(text(query), self)
        captures.append(frame)
        return result

    Connection.exec_driver_sql = intercept
    try:
        stage_d_fallback.run(config_path, working_output_dir)
    finally:
        Connection.exec_driver_sql = original_exec
    if len(captures) != 1:
        raise RuntimeError("Fallback target features were not materialized")
    return captures[0]


def run(config_path: str, private_output_dir: str, confirm: bool) -> dict:
    if not confirm:
        raise ValueError("Export of local row-level clinical features requires --confirm-private")
    out_dir = Path(private_output_dir).resolve()
    if "results" not in out_dir.parts:
        raise ValueError("Private output directory must be under ignored results/")
    if out_dir.exists() and list(out_dir.glob("*.parquet")):
        raise FileExistsError("Refusing to overwrite existing row-level feature extracts")
    # Using a dedicated ignored output for the re-run avoids mutating the original
    # Stage E output folder, but copies the existing preflight metadata only.
    cfg = load_etl_config(config_path)
    if str(cfg.raw["etl"].get("cdm_version", "")).startswith("5.4") is False:
        raise RuntimeError("Wrong frozen CDM version")
    stage_e_output = cfg.audit_dir.parent / "publication_analysis" / "stage_e_statistical_model"
    existing_preflight = stage_e_output / "stage_e_statistical_model_preflight.json"
    if not existing_preflight.exists():
        raise FileNotFoundError("Locked Stage E preflight result is required")
    work_dir = out_dir / "private_rerun"
    work_dir.mkdir(parents=True, exist_ok=True)
    preflight_copy = work_dir / existing_preflight.name
    preflight_copy.write_bytes(existing_preflight.read_bytes())
    source, original_omop = capture_stage_e(config_path, str(work_dir))
    fallback = capture_fallback(config_path, str(work_dir / "fallback"))
    if not set(["patid", "index_date", "outcome", "fixed_population", *FEATURES]).issubset(source.columns):
        raise RuntimeError("Source feature extraction schema changed")
    if not set(["patid", "index_date", "outcome", "fixed_population", *FEATURES]).issubset(original_omop.columns):
        raise RuntimeError("Frozen OMOP feature extraction schema changed")
    shared = source.loc[source.fixed_population == 1].copy()
    data = {
        "pcornet_full": source,
        "pcornet_shared": shared,
        "omop_frozen": original_omop,
        "omop_fallback": fallback,
    }
    # Verify all exact cohort/outcome anchors and matching population index dates
    # in-memory BEFORE writing any private Parquet.
    loaded = {}
    for name, frame in data.items():
        frame = frame[["patid", "index_date", "outcome", *FEATURES]].copy()
        expected_n, expected_events = EXPECTED[name]
        if len(frame) != expected_n or int(frame.outcome.sum()) != expected_events:
            raise RuntimeError(f"{name}: source Stage D anchor mismatch")
        frame["patid"] = frame.patid.astype(str)
        frame["index_date"] = pd.to_datetime(frame.index_date).dt.strftime("%Y-%m-%d")
        frame["is_train"] = frame.patid.map(original_stage_e._stable_train)
        loaded[name] = frame.set_index("patid", drop=False)
    validate_populations(loaded)
    for name, frame in data.items():
        subset = frame[["patid", "index_date", "outcome", *FEATURES]].copy()
        subset.to_parquet(out_dir / f"{name}.parquet", index=False)
    return {
        "status": "private_four_population_feature_export_complete",
        "n": {name: len(frame) for name, frame in data.items()},
        "frozen_etl_sha": FROZEN_SHA,
        "row_level_outputs_local_only": True,
        "do_not_commit_or_upload": True,
        "privacy_review_required": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Secure four-population Stage E feature extraction")
    p.add_argument("--config", required=True, help="Local protected ETL config")
    p.add_argument("--output-dir", default="results/private_inputs")
    p.add_argument("--confirm-private", action="store_true",
                   help="Explicitly authorize row-level LOCAL Parquet files in ignored results/")
    args = p.parse_args()
    status = run(args.config, args.output_dir, args.confirm_private)
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
