# Post-freeze death-aware 90-day outcome sensitivity

**Status:** supplemental, post-freeze analysis. This is not the prespecified Stage D comparison. It must not modify the locked ETL, D0 stroke phenotype, Stage D definitions, or original outputs.

## Reason

The completed 90-day observability/mortality audit found that almost all recorded deaths before day 90 failed the original full 90-day coverage rule. The existing primary result describes people with the required 90-day source enrollment or OMOP observation-period coverage. This sensitivity asks what happens if the observed follow-up can terminate on the recorded death date, provided enrollment/observation covers the period until that date.

## Protocol and scientific choices

- **Cohorts:** Recreate the original source-selected D0 stroke index and exact episode lineage-retained OMOP D0, with no OMOP-native cohort construction. Retain the first qualifying index and original age rule.
- **Time zero:** discharge from the index encounter. The original acute-care outcome uses ED/EI/IP starts during days 1 through 90 inclusive after discharge.
- **Death-aware endpoint:** for patients with a recorded death on or after discharge but before day 90, require **one** PCORnet ENROLLMENT row or **one** OMOP observation_period row to span discharge through the date of death. For everyone else, require coverage through discharge + 90 days. No gaps are bridged. This uses each representation's own recorded death and coverage dates.
- **Death-aware event:** a subsequent acute-care encounter/visit must begin after discharge and on/before the earlier of recorded death or day 90. Patients who die before such an event contribute a negative binary event label. This is a competing-event interpretation, **not** a survival model, a Kaplan-Meier estimate or a causal estimate.
- **Same-day tie:** count a qualifying acute-care event on the recorded calendar date of death, because within-day ordering is not recorded. Report how frequently the first recorded event occurs on the date of death. Review the impact if these counts are non-negligible.
- **Invalid timing:** exclude patients with a recorded death **before index discharge** from this sensitivity, report them separately, and leave them unchanged in the original locked Stage D analysis.
- **Missing death:** a missing death record does not establish survival. These patients continue to require complete recorded 90-day follow-up.
- **Comparison:** report representation-specific eligible populations, event counts, crude binary proportions, OMOP minus PCORnet percentage-point difference and OMOP/PCORnet ratio. Repeat the analysis among shared exact-index patients who satisfy the death-aware coverage rule on **both** sides. Keep the original Stage D results intact.

Because baseline observation windows are defined by distinct representation-specific data structures, changes in eligible denominators are not necessarily caused by death. A death-aware rule does not correct all informative censoring or missingness.

## Source and files

- `scripts/audit_stage_d_90d_observability_mortality.py`: previously validated cohort and baseline event/coverage SQL (read-only, unchanged)
- `scripts/sensitivity_stage_d_deathaware_90d.py`: this sensitivity
- `tests/test_sensitivity_stage_d_deathaware_90d.py`: offline policy, SQL-safety, roll-forward and provenance tests

The script validates the original completed Stage D JSON, then the preceding post-freeze mortality diagnostic. It rebuilds all cohorts/flags and reconciles against the original Stage D and diagnostic anchors before calculating new sensitivity counts. The script only creates SQL Server session-scoped temporary tables and writes an aggregate JSON in the locally gitignored `results/` directory.

## Institutional run

On the machine containing the `OMOP_VALIDATED_A` database, from the audit sensitivity branch and current working `OMOP_SQL_PASSWORD` environment variable:

```bash
cd ~/works/repos/pcornet-omop-validation
source .venv/bin/activate

python -m pytest -q tests/test_sensitivity_stage_d_deathaware_90d.py
PYTHONPATH=src python scripts/sensitivity_stage_d_deathaware_90d.py --self-test

PYTHONPATH=src python scripts/sensitivity_stage_d_deathaware_90d.py \
  --config config/etl_A.yaml \
  --stage-d-summary "/home/asadr/works/pcornet-omop-validation-local-archive-20260907/results/publication_analysis/stage_d_analytical_equivalence/stage_d_stroke_analytical_equivalence.json"
```

`--audit-summary` defaults to the successfully completed local run at:

`results/publication_analysis/postfreeze_observability_mortality/postfreeze_observability_mortality_90d.json`

Use the explicit `--audit-summary PATH` argument only if the completed diagnostic is at another location. Do not rerun the frozen Stage D analysis to recreate its reference file.

**Private local result**:

`results/publication_analysis/postfreeze_deathaware_90d/postfreeze_deathaware_90d_sensitivity.json`

Do not publish, email, commit, or paste the full aggregate JSON until institutional disclosure review approves sharing. Small cells and differencing can disclose restricted information even without patient IDs. This sensitivity does not create a manuscript figure. No source row or patient identifier is exported.

## Acceptance criteria

1. `--self-test` and offline pytest succeed.
2. Original Stage D D0, full-coverage eligibility and outcome anchors reproduce, and the prior mortality diagnostic agrees on coverage, events and early deaths.
3. New eligible/added/removed counts reconcile, and new eligibility is not granted to anyone without a recorded early death who lacked baseline 90-day coverage.
4. All fixed-index four-category outcome counts sum to the fixed eligible population. Result JSON identifies the original reference hashes and analysis Git SHA.
5. Review if many deaths remain unobservable through their recorded death date, many first events have ambiguous death-day ordering, or date-inconsistent records alter the risk difference.

## Interpretation and next action

Report the post-freeze risk estimates descriptively alongside the **unchanged** original Stage D 90-day estimates. A result near the original point estimates would show robustness to this particular death-aware policy, not that informative follow-up selection disappeared. A substantially changed estimate should prompt a manuscript Methods/Limitations revision and discussion with the study team, not an untracked replacement of the original estimand. Avoid applying the original prespecified equivalence thresholds as confirmatory tests for this new sensitivity.