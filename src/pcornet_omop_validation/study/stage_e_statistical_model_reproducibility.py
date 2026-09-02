from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.database import make_engine, table_exists
from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"
STUDY_PATH = Path("study_definitions/stage_e_statistical_model_reproducibility_v1.json")
D0_PATH = Path("study_definitions/stage_c_stroke_d0_v1.json")
STAGE_D_PATH = Path("study_definitions/stage_d_stroke_analytical_equivalence_v1.json")
FEATURES = [
    "age_at_index_years",
    "female_indicator",
    "index_length_of_stay_days",
    "prior_365d_acute_care_encounter_count",
    "prior_365d_all_encounter_count",
    "prior_365d_ischemic_stroke_indicator",
]
CONTINUOUS = [
    "age_at_index_years",
    "index_length_of_stay_days",
    "prior_365d_acute_care_encounter_count",
    "prior_365d_all_encounter_count",
]
BINARY = ["female_indicator", "prior_365d_ischemic_stroke_indicator"]
ACUTE_VISIT_CONCEPT_IDS = (9203, 262, 9201)


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema(v: object) -> str:
    s = str(v or "dbo")
    if not s.replace("_", "a").isalnum() or s[0].isdigit():
        raise ValueError(f"Unsafe schema: {s!r}")
    return s


def _norm(expr: str) -> str:
    return f"REPLACE(UPPER(LTRIM(RTRIM(CONVERT(nvarchar(255), {expr})))),'.','')"


def _short(expr: str) -> str:
    return f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50), {expr}))))"


def _sql_list(values: list[str] | set[str] | tuple[str, ...]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _stable_train(patid: object) -> bool:
    h = hashlib.sha256(str(patid).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % 10 < 7


def _rank_spearman(a: pd.Series, b: pd.Series) -> float | None:
    m = a.notna() & b.notna()
    if int(m.sum()) < 2:
        return None
    ar = a[m].rank(method="average")
    br = b[m].rank(method="average")
    r = ar.corr(br)
    return None if pd.isna(r) else float(r)


def _smd(a: pd.Series, b: pd.Series, binary: bool = False) -> float | None:
    aa = pd.to_numeric(a, errors="coerce").dropna().astype(float)
    bb = pd.to_numeric(b, errors="coerce").dropna().astype(float)
    if len(aa) == 0 or len(bb) == 0:
        return None
    if binary:
        p1, p2 = float(aa.mean()), float(bb.mean())
        den = math.sqrt(max((p1 * (1 - p1) + p2 * (1 - p2)) / 2.0, 0.0))
    else:
        p1, p2 = float(aa.mean()), float(bb.mean())
        den = math.sqrt(max((float(aa.var(ddof=1)) + float(bb.var(ddof=1))) / 2.0, 0.0))
    if den == 0:
        return 0.0 if p1 == p2 else None
    return (p1 - p2) / den


def _continuous_summary(x: pd.Series) -> dict[str, Any]:
    xx = pd.to_numeric(x, errors="coerce")
    y = xx.dropna().astype(float)
    return {
        "n": int(len(xx)),
        "missing_n": int(xx.isna().sum()),
        "mean": None if len(y) == 0 else float(y.mean()),
        "sd": None if len(y) < 2 else float(y.std(ddof=1)),
        "median": None if len(y) == 0 else float(y.median()),
        "q1": None if len(y) == 0 else float(y.quantile(0.25)),
        "q3": None if len(y) == 0 else float(y.quantile(0.75)),
        "min": None if len(y) == 0 else float(y.min()),
        "max": None if len(y) == 0 else float(y.max()),
    }


def _binary_summary(x: pd.Series) -> dict[str, Any]:
    xx = pd.to_numeric(x, errors="coerce")
    y = xx.dropna().astype(float)
    return {
        "n": int(len(xx)),
        "missing_n": int(xx.isna().sum()),
        "count_1": int((y == 1).sum()),
        "proportion_1": None if len(y) == 0 else float((y == 1).mean()),
    }


def _feature_comparison(src: pd.DataFrame, omop: pd.DataFrame, fixed: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in FEATURES:
        binary = f in BINARY
        entry: dict[str, Any] = {
            "source": _binary_summary(src[f]) if binary else _continuous_summary(src[f]),
            "omop": _binary_summary(omop[f]) if binary else _continuous_summary(omop[f]),
            "standardized_mean_difference_source_minus_omop": _smd(src[f], omop[f], binary=binary),
        }
        if fixed:
            s = src.set_index("patid")[f]
            o = omop.set_index("patid")[f]
            ix = s.index.intersection(o.index)
            a, b = s.loc[ix], o.loc[ix]
            both_missing = a.isna() & b.isna()
            both_values = a.notna() & b.notna()
            exact = both_missing | (both_values & (pd.to_numeric(a, errors="coerce") == pd.to_numeric(b, errors="coerce")))
            diff = (pd.to_numeric(a, errors="coerce") - pd.to_numeric(b, errors="coerce")).abs()
            entry["patient_level"] = {
                "shared_n": int(len(ix)),
                "exact_agreement_count": int(exact.sum()),
                "mean_absolute_difference": None if int(diff.notna().sum()) == 0 else float(diff.mean()),
                "median_absolute_difference": None if int(diff.notna().sum()) == 0 else float(diff.median()),
                "spearman_correlation_when_defined": _rank_spearman(pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")),
            }
        out[f] = entry
    return out


def _impute_fit(train: pd.DataFrame, test: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    tr = train[FEATURES].apply(pd.to_numeric, errors="coerce").copy()
    te = test[FEATURES].apply(pd.to_numeric, errors="coerce").copy()
    vals: dict[str, float] = {}
    for f in CONTINUOUS:
        v = float(tr[f].median()) if tr[f].notna().any() else 0.0
        vals[f] = v
        tr[f] = tr[f].fillna(v)
        te[f] = te[f].fillna(v)
    for f in BINARY:
        mode = tr[f].dropna().mode()
        v = float(mode.iloc[0]) if len(mode) else 0.0
        vals[f] = v
        tr[f] = tr[f].fillna(v)
        te[f] = te[f].fillna(v)
    return tr, te, vals


def _calibration(y: np.ndarray, p: np.ndarray) -> dict[str, float | None]:
    from sklearn.linear_model import LogisticRegression

    eps = 1e-6
    pp = np.clip(p.astype(float), eps, 1 - eps)
    z = np.log(pp / (1 - pp)).reshape(-1, 1)
    if len(np.unique(y)) < 2 or float(np.std(z)) == 0.0:
        return {"calibration_intercept": None, "calibration_slope": None}
    m = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
    m.fit(z, y)
    return {"calibration_intercept": float(m.intercept_[0]), "calibration_slope": float(m.coef_[0][0])}


def _prediction_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float | None]:
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    out: dict[str, float | None] = {
        "test_n": int(len(y)),
        "test_events": int(np.sum(y)),
        "test_prevalence": None if len(y) == 0 else float(np.mean(y)),
        "AUROC": None if len(np.unique(y)) < 2 else float(roc_auc_score(y, p)),
        "AUPRC": None if len(np.unique(y)) < 2 else float(average_precision_score(y, p)),
        "Brier_score": None if len(y) == 0 else float(brier_score_loss(y, p)),
    }
    out.update(_calibration(y, p))
    return out


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, model_name: str) -> tuple[np.ndarray, dict[str, Any]]:
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    tr, te, impute = _impute_fit(train, test)
    if model_name == "hist_gradient_boosting":
        model = HistGradientBoostingClassifier(random_state=20260902)
        model.fit(tr[FEATURES], train["outcome"].astype(int).to_numpy())
        p = model.predict_proba(te[FEATURES])[:, 1]
    else:
        prep = ColumnTransformer([
            ("continuous", StandardScaler(), CONTINUOUS),
            ("binary", "passthrough", BINARY),
        ], remainder="drop")
        c = 1e6 if model_name == "logistic_regression" else 1.0
        model = Pipeline([
            ("prep", prep),
            ("model", LogisticRegression(C=c, solver="lbfgs", max_iter=3000, random_state=20260902)),
        ])
        model.fit(tr[FEATURES], train["outcome"].astype(int).to_numpy())
        p = model.predict_proba(te[FEATURES])[:, 1]
    return p, {"imputation_values": impute}


def _association(df: pd.DataFrame) -> dict[str, Any]:
    from sklearn.linear_model import LogisticRegression

    work = df.copy()
    x, _, impute = _impute_fit(work, work)
    means: dict[str, float] = {}
    sds: dict[str, float] = {}
    for f in CONTINUOUS:
        mu = float(x[f].mean())
        sd = float(x[f].std(ddof=0))
        if not np.isfinite(sd) or sd == 0:
            sd = 1.0
        means[f], sds[f] = mu, sd
        x[f] = (x[f] - mu) / sd
    X = x[FEATURES].to_numpy(dtype=float)
    y = work["outcome"].astype(int).to_numpy()
    if len(np.unique(y)) < 2:
        return {"status": "not_estimable_single_outcome_class"}
    model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=3000)
    model.fit(X, y)
    coef = model.coef_[0]
    intercept = float(model.intercept_[0])
    eta = intercept + X @ coef
    p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
    w = p * (1 - p)
    X1 = np.column_stack([np.ones(len(X)), X])
    h = X1.T @ (X1 * w[:, None])
    try:
        cov = np.linalg.pinv(h)
        se = np.sqrt(np.clip(np.diag(cov), 0, np.inf))
    except Exception:
        se = np.full(X1.shape[1], np.nan)
    terms = ["intercept", *FEATURES]
    betas = [intercept, *coef.tolist()]
    rows: dict[str, Any] = {}
    for i, (term, beta) in enumerate(zip(terms, betas)):
        sei = float(se[i]) if np.isfinite(se[i]) else None
        lo = None if sei is None else beta - 1.96 * sei
        hi = None if sei is None else beta + 1.96 * sei
        rows[term] = {
            "coefficient": float(beta),
            "standard_error": sei,
            "odds_ratio": float(math.exp(beta)) if abs(beta) < 700 else None,
            "ci95_lower": None if lo is None or abs(lo) >= 700 else float(math.exp(lo)),
            "ci95_upper": None if hi is None or abs(hi) >= 700 else float(math.exp(hi)),
        }
    return {
        "status": "complete",
        "n": int(len(work)),
        "events": int(y.sum()),
        "continuous_standardization": {f: {"mean": means[f], "sd": sds[f]} for f in CONTINUOUS},
        "imputation_values": impute,
        "terms": rows,
    }


def _association_compare(src: dict[str, Any], omop: dict[str, Any]) -> dict[str, Any]:
    if src.get("status") != "complete" or omop.get("status") != "complete":
        return {"status": "not_comparable"}
    out: dict[str, Any] = {}
    for term in ["intercept", *FEATURES]:
        s = src["terms"][term]
        o = omop["terms"][term]
        out[term] = {
            "source": s,
            "omop": o,
            "coefficient_difference_omop_minus_source": o["coefficient"] - s["coefficient"],
            "odds_ratio_ratio_omop_over_source": None if not s["odds_ratio"] else o["odds_ratio"] / s["odds_ratio"],
        }
    return out


def _probability_agreement(src_test: pd.DataFrame, src_p: np.ndarray, omop_test: pd.DataFrame, omop_p: np.ndarray) -> dict[str, Any]:
    s = pd.DataFrame({"patid": src_test["patid"].astype(str).to_numpy(), "p_source": src_p})
    o = pd.DataFrame({"patid": omop_test["patid"].astype(str).to_numpy(), "p_omop": omop_p})
    m = s.merge(o, on="patid", how="inner")
    if len(m) == 0:
        return {"n": 0}
    d = (m["p_source"] - m["p_omop"]).abs()
    return {
        "n": int(len(m)),
        "pearson_correlation": float(m["p_source"].corr(m["p_omop"])),
        "spearman_correlation": _rank_spearman(m["p_source"], m["p_omop"]),
        "mean_absolute_difference": float(d.mean()),
        "median_absolute_difference": float(d.median()),
        "max_absolute_difference": float(d.max()),
    }


def _prediction_pair(src: pd.DataFrame, omop: pd.DataFrame, fixed: bool) -> dict[str, Any]:
    s = src.copy()
    o = omop.copy()
    s["is_train"] = s["patid"].map(_stable_train)
    o["is_train"] = o["patid"].map(_stable_train)
    out: dict[str, Any] = {}
    for name in ["logistic_regression", "ridge_logistic_regression", "hist_gradient_boosting"]:
        st, se = s[s.is_train].copy(), s[~s.is_train].copy()
        ot, oe = o[o.is_train].copy(), o[~o.is_train].copy()
        sp, smeta = _fit_predict(st, se, name)
        op, ometa = _fit_predict(ot, oe, name)
        entry: dict[str, Any] = {
            "source": _prediction_metrics(se["outcome"].astype(int).to_numpy(), sp),
            "omop": _prediction_metrics(oe["outcome"].astype(int).to_numpy(), op),
            "source_fit": {"train_n": int(len(st)), "train_events": int(st.outcome.sum()), **smeta},
            "omop_fit": {"train_n": int(len(ot)), "train_events": int(ot.outcome.sum()), **ometa},
        }
        for metric in ["AUROC", "AUPRC", "Brier_score", "calibration_intercept", "calibration_slope", "test_prevalence"]:
            sv, ov = entry["source"].get(metric), entry["omop"].get(metric)
            entry.setdefault("differences_omop_minus_source", {})[metric] = None if sv is None or ov is None else ov - sv
        if fixed:
            entry["fixed_cohort_probability_agreement"] = _probability_agreement(se, sp, oe, op)
        out[name] = entry
    return out


def run(config_path: str, output_dir: str | None = None) -> dict[str, Any]:
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding="utf-8"))
    if study.get("status") != "prespecified_before_stage_e_outcome_model_queries":
        raise RuntimeError("Stage E definition is not prespecified")
    if study.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError("Stage E frozen ETL SHA mismatch")
    if study["features"]["core_model_features"] != FEATURES:
        raise RuntimeError("Stage E feature list changed")

    out_dir = Path(output_dir) if output_dir else cfg.audit_dir.parent / "publication_analysis" / "stage_e_statistical_model"
    preflight_path = out_dir / "stage_e_statistical_model_preflight.json"
    if not preflight_path.exists():
        raise RuntimeError("Run Stage E preflight before outcome/model analysis")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight.get("status") != "stage_e_statistical_model_preflight_ready":
        raise RuntimeError("Stage E preflight is not ready")
    if preflight.get("outcome_query_performed") is not False or preflight.get("model_fit_performed") is not False:
        raise RuntimeError("Stage E preflight was not outcome/model free")
    if preflight.get("study_definition_sha256") != _sha256(STUDY_PATH):
        raise RuntimeError("Stage E definition changed after preflight")
    if preflight.get("inherited_d0_definition_sha256") != _sha256(D0_PATH):
        raise RuntimeError("Inherited D0 definition changed after preflight")
    if preflight.get("inherited_stage_d_definition_sha256") != _sha256(STAGE_D_PATH):
        raise RuntimeError("Inherited Stage D definition changed after preflight")

    source_schema = _schema(cfg.raw["sqlserver"].get("source_schema", "dbo"))
    target_schema = _schema(cfg.raw["sqlserver"].get("target_schema", "dbo"))
    all_stroke = _sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))
    acute_ids = ",".join(str(x) for x in ACUTE_VISIT_CONCEPT_IDS)

    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            required = [
                (source_schema, "PCORnet_DEMOGRAPHIC"), (source_schema, "PCORnet_ENCOUNTER"),
                (source_schema, "PCORnet_DIAGNOSIS"), (source_schema, "PCORnet_ENROLLMENT"),
                (target_schema, "person"), (target_schema, "visit_occurrence"),
                (target_schema, "condition_occurrence"), (target_schema, "observation_period"),
                (target_schema, "etl_visit_occurrence_xwalk"), (target_schema, "etl_condition_occurrence_xwalk"),
            ]
            for schema, table in required:
                if not table_exists(con, schema, table):
                    raise RuntimeError(f"Missing required table [{schema}].[{table}]")

            print("progress: reproducing source and lineage-faithful OMOP D0 cohorts", flush=True)
            con.exec_driver_sql(f"""
                IF OBJECT_ID('tempdb..#d0_candidates') IS NOT NULL DROP TABLE #d0_candidates;
                ;WITH dx_rank AS (
                  SELECT CONVERT(nvarchar(255),d.PATID) patid,CONVERT(nvarchar(255),d.ENCOUNTERID) encounterid,
                         CONVERT(nvarchar(255),d.DIAGNOSISID) diagnosisid,CAST(d.DX_DATE AS date) dx_date,
                         ROW_NUMBER() OVER (PARTITION BY CONVERT(nvarchar(255),d.PATID),CONVERT(nvarchar(255),d.ENCOUNTERID)
                           ORDER BY CASE WHEN d.DX_DATE IS NULL THEN 1 ELSE 0 END,CAST(d.DX_DATE AS date),{_norm('d.DX')},CONVERT(nvarchar(255),d.DIAGNOSISID)) rn
                  FROM [{source_schema}].[PCORnet_DIAGNOSIS] d
                  WHERE {_norm('d.DX')} IN ({all_stroke}) AND {_short('d.PDX')}='P'
                )
                SELECT x.patid,x.encounterid,x.diagnosisid,x.dx_date,CAST(e.ADMIT_DATE AS date) admit_date,
                       CAST(e.DISCHARGE_DATE AS date) discharge_date,
                       COALESCE(x.dx_date,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date)) index_date,
                       CAST(dm.BIRTH_DATE AS date) birth_date, {_short('dm.SEX')} sex
                INTO #d0_candidates
                FROM dx_rank x
                JOIN [{source_schema}].[PCORnet_ENCOUNTER] e ON CONVERT(nvarchar(255),e.PATID)=x.patid AND CONVERT(nvarchar(255),e.ENCOUNTERID)=x.encounterid
                JOIN [{source_schema}].[PCORnet_DEMOGRAPHIC] dm ON CONVERT(nvarchar(255),dm.PATID)=x.patid
                WHERE x.rn=1 AND {_short('e.ENC_TYPE')} IN ('EI','IP') AND e.ADMIT_DATE IS NOT NULL AND e.DISCHARGE_DATE IS NOT NULL
                  AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),CAST(e.DISCHARGE_DATE AS date))>=1;
                CREATE INDEX IX_d0cand ON #d0_candidates(patid,index_date,encounterid);

                IF OBJECT_ID('tempdb..#src_d0') IS NOT NULL DROP TABLE #src_d0;
                ;WITH q AS (
                  SELECT *,ROW_NUMBER() OVER (PARTITION BY patid ORDER BY index_date,encounterid) rn FROM #d0_candidates WHERE index_date IS NOT NULL
                )
                SELECT * INTO #src_d0 FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_srcd0 ON #src_d0(patid);

                IF OBJECT_ID('tempdb..#omop_d0') IS NOT NULL DROP TABLE #omop_d0;
                ;WITH q AS (
                  SELECT d.patid,p.person_id,v.visit_occurrence_id,CAST(v.visit_start_date AS date) admit_date,
                         CAST(v.visit_end_date AS date) discharge_date,CAST(p.birth_datetime AS date) birth_date,
                         p.gender_concept_id,
                         COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)) index_date,
                         ROW_NUMBER() OVER (PARTITION BY d.patid ORDER BY COALESCE(CAST(co.condition_start_date AS date),CAST(v.visit_start_date AS date),CAST(v.visit_end_date AS date)),v.visit_occurrence_id) rn
                  FROM #d0_candidates d
                  JOIN [{target_schema}].[person] p ON CONVERT(nvarchar(255),p.person_source_value)=d.patid
                  JOIN [{target_schema}].[etl_visit_occurrence_xwalk] vx ON CONVERT(nvarchar(255),vx.encounterid)=d.encounterid
                  JOIN [{target_schema}].[visit_occurrence] v ON v.visit_occurrence_id=vx.visit_occurrence_id AND v.person_id=p.person_id
                  JOIN [{target_schema}].[etl_condition_occurrence_xwalk] cx ON cx.source_domain='DIAGNOSIS' AND CONVERT(nvarchar(255),cx.source_record_id)=d.diagnosisid
                  JOIN [{target_schema}].[condition_occurrence] co ON co.condition_occurrence_id=cx.condition_occurrence_id AND co.person_id=p.person_id AND co.visit_occurrence_id=v.visit_occurrence_id
                )
                SELECT * INTO #omop_d0 FROM q WHERE rn=1 AND FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;
                CREATE UNIQUE CLUSTERED INDEX IX_omopd0 ON #omop_d0(patid);
            """)

            print("progress: applying 90-day observability and outcome definitions", flush=True)
            con.exec_driver_sql(f"""
                IF OBJECT_ID('tempdb..#src90') IS NOT NULL DROP TABLE #src90;
                SELECT s.*,
                  CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENCOUNTER] e
                    WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND e.ADMIT_DATE IS NOT NULL
                      AND DATEDIFF(day,s.discharge_date,CAST(e.ADMIT_DATE AS date)) BETWEEN 1 AND 90
                      AND {_short('e.ENC_TYPE')} IN ('ED','EI','IP')) THEN 1 ELSE 0 END outcome
                INTO #src90 FROM #src_d0 s
                WHERE EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_ENROLLMENT] en
                  WHERE CONVERT(nvarchar(255),en.PATID)=s.patid AND en.ENR_START_DATE<=s.discharge_date AND en.ENR_END_DATE>=DATEADD(day,90,s.discharge_date));
                CREATE UNIQUE CLUSTERED INDEX IX_src90 ON #src90(patid);

                IF OBJECT_ID('tempdb..#omop90') IS NOT NULL DROP TABLE #omop90;
                SELECT o.*,
                  CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[visit_occurrence] v
                    WHERE v.person_id=o.person_id AND v.visit_start_date IS NOT NULL
                      AND DATEDIFF(day,o.discharge_date,CAST(v.visit_start_date AS date)) BETWEEN 1 AND 90
                      AND v.visit_concept_id IN ({acute_ids})) THEN 1 ELSE 0 END outcome
                INTO #omop90 FROM #omop_d0 o
                WHERE EXISTS (SELECT 1 FROM [{target_schema}].[observation_period] op
                  WHERE op.person_id=o.person_id AND op.observation_period_start_date<=o.discharge_date AND op.observation_period_end_date>=DATEADD(day,90,o.discharge_date));
                CREATE UNIQUE CLUSTERED INDEX IX_omop90 ON #omop90(patid);

                IF OBJECT_ID('tempdb..#fixed') IS NOT NULL DROP TABLE #fixed;
                SELECT s.patid INTO #fixed FROM #src90 s JOIN #omop90 o ON o.patid=s.patid AND o.index_date=s.index_date;
                CREATE UNIQUE CLUSTERED INDEX IX_fixed ON #fixed(patid);
            """)

            counts = con.execute(text("""
                SELECT
                  (SELECT COUNT_BIG(*) FROM #src_d0) source_d0,
                  (SELECT COUNT_BIG(*) FROM #omop_d0) omop_d0,
                  (SELECT COUNT_BIG(*) FROM #src90) source_90_eligible,
                  (SELECT SUM(outcome) FROM #src90) source_90_events,
                  (SELECT COUNT_BIG(*) FROM #omop90) omop_90_eligible,
                  (SELECT SUM(outcome) FROM #omop90) omop_90_events,
                  (SELECT COUNT_BIG(*) FROM #fixed) fixed_eligible,
                  (SELECT SUM(s.outcome) FROM #src90 s JOIN #fixed f ON f.patid=s.patid) fixed_source_events,
                  (SELECT SUM(o.outcome) FROM #omop90 o JOIN #fixed f ON f.patid=o.patid) fixed_omop_events
            """)).mappings().one()
            anchors = {k: int(v or 0) for k, v in dict(counts).items()}
            expected = {
                "source_d0": 9815, "omop_d0": 6001,
                "source_90_eligible": 6508, "source_90_events": 1798,
                "omop_90_eligible": 3822, "omop_90_events": 1132,
                "fixed_eligible": 3822, "fixed_source_events": 1132, "fixed_omop_events": 1132,
            }
            if anchors != expected:
                raise RuntimeError(f"Stage E failed to reproduce locked Stage C/D anchors: observed={anchors}, expected={expected}")

            print("progress: constructing native PCORnet and OMOP features", flush=True)
            src = pd.read_sql(text(f"""
                SELECT s.patid,s.outcome,
                  CAST(FLOOR(DATEDIFF(day,s.birth_date,s.index_date)/365.0) AS float) age_at_index_years,
                  CASE WHEN s.sex='F' THEN 1.0 WHEN s.sex='M' THEN 0.0 ELSE NULL END female_indicator,
                  CAST(DATEDIFF(day,s.admit_date,s.discharge_date) AS float) index_length_of_stay_days,
                  CAST((SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_ENCOUNTER] e WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND e.ADMIT_DATE IS NOT NULL AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),s.index_date) BETWEEN 1 AND 365 AND {_short('e.ENC_TYPE')} IN ('ED','EI','IP')) AS float) prior_365d_acute_care_encounter_count,
                  CAST((SELECT COUNT_BIG(*) FROM [{source_schema}].[PCORnet_ENCOUNTER] e WHERE CONVERT(nvarchar(255),e.PATID)=s.patid AND e.ADMIT_DATE IS NOT NULL AND DATEDIFF(day,CAST(e.ADMIT_DATE AS date),s.index_date) BETWEEN 1 AND 365) AS float) prior_365d_all_encounter_count,
                  CASE WHEN EXISTS (SELECT 1 FROM [{source_schema}].[PCORnet_DIAGNOSIS] d WHERE CONVERT(nvarchar(255),d.PATID)=s.patid AND d.DX_DATE IS NOT NULL AND DATEDIFF(day,CAST(d.DX_DATE AS date),s.index_date) BETWEEN 1 AND 365 AND {_norm('d.DX')} IN ({all_stroke})) THEN 1.0 ELSE 0.0 END prior_365d_ischemic_stroke_indicator,
                  CASE WHEN f.patid IS NULL THEN 0 ELSE 1 END fixed_population
                FROM #src90 s LEFT JOIN #fixed f ON f.patid=s.patid
            """), con)
            omop = pd.read_sql(text(f"""
                SELECT o.patid,o.outcome,
                  CAST(FLOOR(DATEDIFF(day,o.birth_date,o.index_date)/365.0) AS float) age_at_index_years,
                  CASE WHEN o.gender_concept_id=8532 THEN 1.0 WHEN o.gender_concept_id=8507 THEN 0.0 ELSE NULL END female_indicator,
                  CAST(DATEDIFF(day,o.admit_date,o.discharge_date) AS float) index_length_of_stay_days,
                  CAST((SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence] v WHERE v.person_id=o.person_id AND v.visit_start_date IS NOT NULL AND DATEDIFF(day,CAST(v.visit_start_date AS date),o.index_date) BETWEEN 1 AND 365 AND v.visit_concept_id IN ({acute_ids})) AS float) prior_365d_acute_care_encounter_count,
                  CAST((SELECT COUNT_BIG(*) FROM [{target_schema}].[visit_occurrence] v WHERE v.person_id=o.person_id AND v.visit_start_date IS NOT NULL AND DATEDIFF(day,CAST(v.visit_start_date AS date),o.index_date) BETWEEN 1 AND 365) AS float) prior_365d_all_encounter_count,
                  CASE WHEN EXISTS (SELECT 1 FROM [{target_schema}].[condition_occurrence] c WHERE c.person_id=o.person_id AND c.condition_start_date IS NOT NULL AND DATEDIFF(day,CAST(c.condition_start_date AS date),o.index_date) BETWEEN 1 AND 365 AND {_norm('c.condition_source_value')} IN ({all_stroke})) THEN 1.0 ELSE 0.0 END prior_365d_ischemic_stroke_indicator,
                  CASE WHEN f.patid IS NULL THEN 0 ELSE 1 END fixed_population
                FROM #omop90 o LEFT JOIN #fixed f ON f.patid=o.patid
            """), con)
    finally:
        engine.dispose()

    src["patid"] = src["patid"].astype(str)
    omop["patid"] = omop["patid"].astype(str)
    src_fixed = src[src.fixed_population == 1].copy()
    omop_fixed = omop[omop.fixed_population == 1].copy()

    print("progress: computing descriptive reproducibility", flush=True)
    descriptive = {
        "fixed_cohort": _feature_comparison(src_fixed, omop_fixed, fixed=True),
        "end_to_end": _feature_comparison(src, omop, fixed=False),
    }

    print("progress: fitting association models", flush=True)
    assoc_fixed_s = _association(src_fixed)
    assoc_fixed_o = _association(omop_fixed)
    assoc_e2e_s = _association(src)
    assoc_e2e_o = _association(omop)
    associations = {
        "fixed_cohort": _association_compare(assoc_fixed_s, assoc_fixed_o),
        "end_to_end": _association_compare(assoc_e2e_s, assoc_e2e_o),
    }

    print("progress: fitting locked prediction models", flush=True)
    predictions = {
        "fixed_cohort": _prediction_pair(src_fixed, omop_fixed, fixed=True),
        "end_to_end": _prediction_pair(src, omop, fixed=False),
    }

    payload = {
        "status": "stage_e_statistical_model_reproducibility_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "study_definition": study["study_definition"],
        "study_definition_sha256": _sha256(STUDY_PATH),
        "inherited_d0_definition_sha256": _sha256(D0_PATH),
        "inherited_stage_d_definition_sha256": _sha256(STAGE_D_PATH),
        "preflight_analysis_git_sha": preflight.get("analysis_git_sha"),
        "analysis_git_sha": _git("rev-parse", "HEAD"),
        "analysis_worktree_clean": _git("status", "--porcelain") == "",
        "locked_anchor_reproduction": anchors,
        "population_sizes": {"fixed": len(src_fixed), "source_end_to_end": len(src), "omop_end_to_end": len(omop)},
        "descriptive_reproducibility": descriptive,
        "association_reproducibility": associations,
        "prediction_reproducibility": predictions,
        "interpretation_guardrails": study["interpretation_guardrails"],
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "row_level_predictions_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "stage_e_statistical_model_reproducibility.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print("status: stage_e_statistical_model_reproducibility_complete")
    print(f"locked_anchor_reproduction: {anchors}")
    print(f"output: {out}")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Stage E statistical and prediction-model reproducibility")
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()
