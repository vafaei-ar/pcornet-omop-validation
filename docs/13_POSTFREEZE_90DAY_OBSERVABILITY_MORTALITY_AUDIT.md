# Post-freeze 90-day observability and mortality audit

This is a **post-freeze, descriptive diagnostic**, not a replacement for the prespecified Stage D D0 cohorts, acute-care outcome, eligibility rule, or equivalence estimates. Run it only against the authorized institutional SQL Server environment.

## Repo files

- `scripts/audit_stage_d_90d_observability_mortality.py`: audit runner
- `tests/test_audit_stage_d_90d.py`: offline unit tests
- `docs/13_POSTFREEZE_90DAY_OBSERVABILITY_MORTALITY_AUDIT.md`: this guide

## Prerequisites

Use the established institutional Python environment and the same `config/etl.yaml` (or `config/etl_A.yaml`) and source/OMOP schemas used for Stage D. The SQL Server password environment variable must be set but must not be printed. The original completed Stage D JSON must be present locally at:

`results/publication_analysis/stage_d_analytical_equivalence/stage_d_stroke_analytical_equivalence.json`

Do **not** rerun Stage D to recreate this file. If it exists in a different authorized location, pass `--stage-d-summary PATH` to the audit.

## Run from the repository root

```bash
cd ~/works/repos/pcornet-omop-validation
source .venv/bin/activate

python -m pytest -q tests/test_audit_stage_d_90d.py
PYTHONPATH=src python scripts/audit_stage_d_90d_observability_mortality.py --self-test
PYTHONPATH=src python scripts/audit_stage_d_90d_observability_mortality.py --config config/etl.yaml
```

The main run requires SQLAlchemy, pyodbc, valid credentials, and read access to the source/target schemas. A longer runtime is possible because it reconstructs the original D0 cohorts in **connection-scoped `#temp` tables**. It never creates permanent SQL tables or writes back to patient data.

## Guardrails

1. Require the same frozen ETL SHA and SHA256 hashes for the Stage D and D0 study definitions as the completed parent Stage D result.
2. Reproduce these locked source/target cohort and 90-day numerator/denominator anchors: source D0 9,815; retained OMOP D0 6,001; fixed-index eligible 3,822; source end-to-end eligible/events 6,508/1,798; OMOP end-to-end eligible/events 3,822/1,132.
3. Stop on any mismatch. Do not silently re-anchor to new estimates.
4. The diagnostic counts observed events even without full coverage and links recorded deaths to coverage strata, but it does not amend Stage D event eligibility.
5. Recorded death before day 90 does not itself establish that death caused loss of coverage. Missing death records do not prove survival.

## Private output

By default the script writes to the repo-ignored path:

`results/publication_analysis/postfreeze_observability_mortality/postfreeze_observability_mortality_90d.json`

This output contains **aggregate results only**, grouped by source D0, lineage-retained OMOP D0, source-only patients, coverage status, admission era, and coverage combinations. It reports deaths before day 90, events outside the full-coverage denominator, and mean index age.

**Do not commit, email, or upload the raw JSON without institutional disclosure approval.** Cross-tabulated summaries may contain small or reconstructible cells even if no patient IDs are saved. If approval is not granted, only share approved disclosure-safe summaries or interpretive findings. Avoid sharing credentials, server addresses, or connection strings.

## Interpretation

If the anchors match, report whether early deaths disproportionately appear among patients not meeting 90-day follow-up and whether observable populations differ in admission era and age. This is a descriptive post-freeze audit and must not replace the frozen estimands or support causal assertions about death and observability. If a follow-up sensitivity analysis is needed, prespecify and document it separately.