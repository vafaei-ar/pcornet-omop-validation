from __future__ import annotations

"""Compatibility wrapper for the locked Stage E analysis.

The first Stage E execution attempt stopped BEFORE feature/model fitting because the
base analysis reconstructed lineage-faithful OMOP D0 by selecting the earliest
surviving materialized OMOP episode per patient. Locked Stage C instead first selects
the source D0 index episode, then asks whether THAT EXACT episode materialized in OMOP.

Why a wrapper instead of silently editing the locked analysis
-------------------------------------------------------------
The Stage E study definition, feature set, outcome, patient split, models, and metrics
had already been prespecified. The safest correction was therefore to preserve the
original module and apply the smallest execution-time compatibility patch needed to
restore the inherited Stage C cohort semantics. This makes the correction explicit in
Git history and prevents it from being mistaken for post-hoc model tuning.

What this wrapper changes
-------------------------
Only the SQL fragment that builds #omop_d0:
- source is changed from all D0 candidates to the already selected #src_d0 episode;
- the redundant adult-age filter is removed because #src_d0 already applied it.

What this wrapper does NOT change
---------------------------------
- the frozen ETL;
- the Stage E study definition;
- the outcome;
- any feature definition;
- imputation/scaling;
- train/test hashing;
- prediction models;
- evaluation metrics.

The completed Stage E run must still reproduce all locked Stage C/D anchors before any
feature comparison or model result is accepted.
"""

from sqlalchemy.engine import Connection

from pcornet_omop_validation.study import stage_e_statistical_model_reproducibility as base


# Keep a stable reference so the SQLAlchemy method is always restored, including when
# base.run raises an exception. This avoids leaking the compatibility patch into other
# analyses executed in the same Python process.
_ORIGINAL = Connection.exec_driver_sql


def _patched_exec_driver_sql(self: Connection, statement: str, *args, **kwargs):
    """Patch only the known Stage E OMOP-D0 construction statement.

    The marker and exact old fragments make this intentionally brittle: if the base SQL
    changes, fail loudly rather than applying a patch to an unrecognized query.
    """
    sql = statement
    marker = "IF OBJECT_ID('tempdb..#omop_d0') IS NOT NULL DROP TABLE #omop_d0;"
    if marker in sql:
        old_from = "FROM #d0_candidates d\n                  JOIN"
        new_from = "FROM #src_d0 d\n                  JOIN"
        old_where = (
            "SELECT * INTO #omop_d0 FROM q WHERE rn=1 AND "
            "FLOOR(DATEDIFF(day,birth_date,index_date)/365.0)>=18;"
        )
        new_where = "SELECT * INTO #omop_d0 FROM q WHERE rn=1;"
        if old_from not in sql or old_where not in sql:
            raise RuntimeError("Stage E anchor-fix wrapper could not locate the expected locked D0 SQL")
        sql = sql.replace(old_from, new_from, 1).replace(old_where, new_where, 1)
    return _ORIGINAL(self, sql, *args, **kwargs)


def run(config_path: str, output_dir: str | None = None):
    # The monkeypatch is scoped to this one run and restored in finally. No caller should
    # import this module expecting a globally modified SQLAlchemy connection behavior.
    Connection.exec_driver_sql = _patched_exec_driver_sql
    try:
        return base.run(config_path, output_dir)
    finally:
        Connection.exec_driver_sql = _ORIGINAL


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="Stage E statistical/model reproducibility with locked Stage C D0 anchor correction"
    )
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir")
    a = p.parse_args()
    run(a.config, a.output_dir)


if __name__ == "__main__":
    main()