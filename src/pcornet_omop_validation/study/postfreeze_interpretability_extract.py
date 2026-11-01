from __future__ import annotations

"""Private, guarded four-cohort feature exporter for post-freeze interpretability.

Runs the unchanged locked Stage E and post-freeze Stage D fallback programs.
Uses narrowly scoped, fail-closed read interception to copy native features
while their existing SQL Server #temp tables are alive. Never commits or
publishes these row-level local files. No frozen ETL or study definitions change.
"""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import inspect
import json
import os
import stat
import sys
from urllib.parse import quote_plus
import numpy as np
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
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


ROOT = Path(__file__).resolve().parents[3]
PRIVATE_ROOT = ROOT / "results"


def _private_output_path(private_output_dir: str) -> Path:
    """Check that output is truly inside repo results, not a named lookalike."""
    raw = Path(private_output_dir).expanduser()
    candidate = Path(os.path.abspath(raw))
    private_root = Path(os.path.abspath(PRIVATE_ROOT))
    if candidate == private_root or not candidate.is_relative_to(private_root):
        raise ValueError("Private output directory must be under ignored results/")
    current = private_root
    if current.is_symlink():
        raise ValueError("Private output path must not contain symlinks")
    for part in candidate.relative_to(private_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Private output path must not contain symlinks")
    if not candidate.resolve().is_relative_to(private_root.resolve()):
        raise ValueError("Private output directory escapes ignored results/")
    return candidate


def _prepare_private_output(out_dir: Path) -> Path:
    """Create only an empty 0700 export directory and a clean 0700 workdir."""
    if out_dir.is_symlink():
        raise ValueError("Private output cannot be a symlink")
    if out_dir.exists():
        if not out_dir.is_dir() or stat.S_IMODE(out_dir.stat().st_mode) != 0o700:
            raise ValueError("Existing private output directory must be mode 0700")
        if any(out_dir.iterdir()):
            raise FileExistsError(
                "Private output directory is not empty; preserve existing outputs"
            )
    else:
        out_dir.mkdir(parents=True, mode=0o700)
    # Explicitly check permissions after creation. Never widen permissions.
    if stat.S_IMODE(out_dir.stat().st_mode) != 0o700:
        raise ValueError("Private output directory must be mode 0700")
    work_dir = out_dir / "private_rerun"
    work_dir.mkdir(mode=0o700)
    if stat.S_IMODE(work_dir.stat().st_mode) != 0o700:
        raise ValueError("Private rerun directory must be mode 0700")
    return work_dir


def _write_private_parquet(frame: pd.DataFrame, destination: Path) -> None:
    """Create-only binary file; PHI extracts never inherit a permissive umask."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(destination, flags, 0o600), "wb") as handle:
        frame.to_parquet(handle, index=False)
        handle.flush()
        os.fsync(handle.fileno())
    if stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise ValueError("Private Parquet must be mode 0600")


def _prepare_population(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    """Apply the model's exact input hygiene and frozen anchors in-memory."""
    expected_n, expected_events = EXPECTED[name]
    required = {"patid", "index_date", "outcome", *FEATURES}
    if not required.issubset(frame.columns):
        raise RuntimeError(f"{name}: feature extraction schema changed")
    subset = frame[["patid", "index_date", "outcome", *FEATURES]].copy()
    if subset.patid.isna().any():
        raise RuntimeError(f"{name}: null patient ID")
    subset["patid"] = subset.patid.astype(str)
    if subset.patid.duplicated().any():
        raise RuntimeError(f"{name}: duplicated patient ID")
    dates = pd.to_datetime(subset.index_date, errors="coerce")
    if dates.isna().any():
        raise RuntimeError(f"{name}: invalid index date")
    subset["index_date"] = dates.dt.strftime("%Y-%m-%d")
    subset["outcome"] = pd.to_numeric(subset.outcome, errors="coerce")
    if subset.outcome.isna().any() or not subset.outcome.isin([0, 1]).all():
        raise RuntimeError(f"{name}: invalid binary outcome")
    subset["outcome"] = subset.outcome.astype(int)
    for feature in FEATURES:
        subset[feature] = pd.to_numeric(subset[feature], errors="raise")
        nonmissing = subset[feature].dropna().to_numpy(dtype=float)
        if not np.isfinite(nonmissing).all():
            raise RuntimeError(f"{name}: non-finite feature value")
        if feature in ("female_indicator", "prior_365d_ischemic_stroke_indicator"):
            if not subset[feature].dropna().isin((0, 1)).all():
                raise RuntimeError(f"{name}: invalid binary feature")
    if len(subset) != expected_n or int(subset.outcome.sum()) != expected_events:
        raise RuntimeError(f"{name}: source Stage D anchor mismatch")
    subset["is_train"] = subset.patid.map(original_stage_e._stable_train)
    return subset.set_index("patid", drop=False)



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




def preflight_private_export(config_path: str, private_output_dir: str) -> dict:
    """Strict NO-SQL/NO-WRITE preflight of prior locked artifacts and code hooks."""
    out_dir = _private_output_path(private_output_dir)
    if out_dir.exists():
        if not out_dir.is_dir() or stat.S_IMODE(out_dir.stat().st_mode) != 0o700:
            raise RuntimeError("Private export directory must be mode 0700")
        if any(out_dir.iterdir()):
            raise FileExistsError("Private export directory not empty; do not overwrite")

    cfg = load_etl_config(config_path)  # No DB connection.
    if not str(cfg.raw["etl"].get("cdm_version", "")).startswith("5.4"):
        raise RuntimeError("Unexpected frozen OMOP CDM version")
    preflight_path = (
        cfg.audit_dir.parent / "publication_analysis"
        / "stage_e_statistical_model" / "stage_e_statistical_model_preflight.json"
    )
    if not preflight_path.is_file():
        raise RuntimeError("Locked Stage E preflight file is missing")
    try:
        prior = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Locked Stage E preflight is unreadable") from None
    if (prior.get("status") != "stage_e_statistical_model_preflight_ready"
            or prior.get("outcome_query_performed") is not False
            or prior.get("model_fit_performed") is not False):
        raise RuntimeError("Saved Stage E preflight is not outcome-free and ready")
    for key, path in (
        ("study_definition_sha256", original_stage_e.STUDY_PATH),
        ("inherited_d0_definition_sha256", original_stage_e.D0_PATH),
        ("inherited_stage_d_definition_sha256", original_stage_e.STAGE_D_PATH),
    ):
        if not path.is_file() or prior.get(key) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise RuntimeError("Locked Stage E preflight definition hash mismatch")

    fallback_record = stage_d_fallback.STAGE_C_RESULT_PATH
    if not fallback_record.is_file():
        raise RuntimeError("Completed Stage C fallback evidence missing")
    try:
        prior_fallback = json.loads(fallback_record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Saved Stage C fallback evidence unreadable") from None
    if (prior_fallback.get("status")
            != "stage_c_encounter_date_fallback_sensitivity_complete"
            or prior_fallback.get("frozen_etl_sha") != FROZEN_SHA):
        raise RuntimeError("Stage C fallback artifact does not match frozen ETL")

    stage_e_code = inspect.getsource(original_stage_e.run)
    fallback_code = inspect.getsource(stage_d_fallback.run)
    if (stage_e_code.count("SELECT s.patid,s.outcome,") != 1
            or stage_e_code.count("SELECT o.patid,o.outcome,") != 1
            or "FROM #src90 s" not in stage_e_code
            or "FROM #omop90 o" not in stage_e_code
            or "INTO #fd_tgt_labels" not in fallback_code):
        raise RuntimeError("Locked SQL capture hooks do not match expected code")
    for package in (
        "pyarrow", "scikit-learn", "xgboost", "shap",
    ):
        try:
            importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError("Missing required interpretability package: " + package) from None

    return {
        "status": "PRIVATE RF XGBOOST PREFLIGHT PASS",
        "database_queries_performed": False,
        "private_files_created": False,
        "locked_definitions_validated": True,
        "private_path_checked": True,
        "dependency_check_passed": True,
    }



@contextmanager
def restricted_stage_engines(config_path: str):
    """Use only the locked read-only QC principal for both unchanged analyses.

    The locked analyses call their imported make_engine() bindings. Override
    those bindings narrowly and always restore them, including on errors.
    SQL may create session-local #temp tables but never permanent CDM tables.
    """
    from pcornet_omop_validation.study.postfreeze_restricted_qc import (
        FROZEN_DATABASE,
        inspect_frozen,
        make_restricted_odbc_string,
        verified_server_config,
    )
    import pyodbc

    server = verified_server_config(config_path)
    secret = os.getenv("OHDSI_QC_PASSWORD", "")
    if not secret:
        raise RuntimeError("OHDSI_QC_PASSWORD is required for protected extraction")
    odbc = make_restricted_odbc_string(server, FROZEN_DATABASE, secret)
    db_connection = pyodbc.connect(odbc, autocommit=True, timeout=20)
    try:
        inspect_frozen(db_connection)
    finally:
        db_connection.close()

    def restricted_factory(_cfg):
        return create_engine(
            "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc),
            future=True,
            pool_pre_ping=True,
        )

    original_stage_factory = original_stage_e.make_engine
    original_fallback_factory = stage_d_fallback.make_engine
    original_stage_e.make_engine = restricted_factory
    stage_d_fallback.make_engine = restricted_factory
    try:
        yield
    finally:
        original_stage_e.make_engine = original_stage_factory
        stage_d_fallback.make_engine = original_fallback_factory



def run(config_path: str, private_output_dir: str, confirm: bool) -> dict:
    if not confirm:
        raise ValueError("Export of local row-level clinical features requires --confirm-private")
    # Refuse an incomplete, altered or unavailable historical preflight before
    # any clinical query or creation of private files.
    preflight_private_export(config_path, private_output_dir)
    out_dir = _private_output_path(private_output_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError("Private output directory is not empty; preserve existing outputs")
    # Using a dedicated ignored output for the re-run avoids mutating the original
    # Stage E output folder, but copies the existing preflight metadata only.
    cfg = load_etl_config(config_path)
    if str(cfg.raw["etl"].get("cdm_version", "")).startswith("5.4") is False:
        raise RuntimeError("Wrong frozen CDM version")
    stage_e_output = cfg.audit_dir.parent / "publication_analysis" / "stage_e_statistical_model"
    existing_preflight = stage_e_output / "stage_e_statistical_model_preflight.json"
    if not existing_preflight.exists():
        raise FileNotFoundError("Locked Stage E preflight result is required")
    work_dir = _prepare_private_output(out_dir)
    preflight_copy = work_dir / existing_preflight.name
    preflight_copy.write_bytes(existing_preflight.read_bytes())
    # Both the original Stage E and fallback Stage D computations must run
    # under the restricted QC login, not the ETL/admin login from config.
    with restricted_stage_engines(config_path):
        source, original_omop = capture_stage_e(config_path, str(work_dir))
        fallback_dir = work_dir / "fallback"
        fallback_dir.mkdir(mode=0o700)
        if stat.S_IMODE(fallback_dir.stat().st_mode) != 0o700:
            raise RuntimeError("Fallback working directory must be mode 0700")
        fallback = capture_fallback(config_path, str(fallback_dir))
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
    # Verify patient identities, features, frozen anchors and all four
    # relationships BEFORE any private Parquet is materialized.
    loaded = {
        name: _prepare_population(frame, name) for name, frame in data.items()
    }
    validate_populations(loaded)
    for name, frame in loaded.items():
        _write_private_parquet(
            frame[["patid", "index_date", "outcome", *FEATURES]],
            out_dir / f"{name}.parquet",
        )
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
    p.add_argument("--preflight-only", action="store_true",
                   help="Read-only local provenance/dependency check; no SQL or file writes")
    args = p.parse_args()
    if args.preflight_only:
        status = preflight_private_export(args.config, args.output_dir)
    else:
        try:
            status = run(args.config, args.output_dir, args.confirm_private)
        except Exception:
            # SQLAlchemy/ODBC provider exceptions can embed usernames, DSNs,
            # passwords, source queries or patient-level details. Do not print
            # raw exception text or traceback on a protected clinical run.
            print(
                "PROTECTED FOUR-COHORT EXPORT BLOCKED: sensitive diagnostics "
                "suppressed. Preserve local outputs and review locally; "
                "do not upload logs or partial extracts.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
