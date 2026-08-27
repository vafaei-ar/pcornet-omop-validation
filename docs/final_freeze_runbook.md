# Final publication-freeze runbook

This runbook is the operational checklist for the final code-frozen PCORnet-to-OMOP rebuild. It is intentionally conservative: the historical/comparator OMOP database is never discovered, reset, truncated, or modified by this procedure.

## Preconditions

Use the publication configuration that points to the isolated validated target (`config/etl_A.yaml` in the current environment). Do not substitute the older development configuration.

The final rebuild should start only after all intended ETL and audit code changes are committed and the local worktree is clean. The Phase 14 manifest records local Git status explicitly, so uncommitted changes would make the build unsuitable as the publication freeze even if semantic validation passes.

## 1. Synchronize the repository safely

First inspect local changes; do not discard them automatically:

```bash
cd ~/works/repos/pcornet-omop-validation
git status --short
git branch --show-current
```

The intended branch is:

```text
audit/etl-freeze-candidate
```

Fetch the remote branch:

```bash
git fetch origin audit/etl-freeze-candidate
```

If the worktree is clean, fast-forward only:

```bash
git pull --ff-only origin audit/etl-freeze-candidate
```

If the worktree is not clean, inspect and preserve the local changes deliberately before pulling. Do not use `git reset --hard` merely to make the tree clean.

Record the candidate publication commit:

```bash
git rev-parse HEAD
git status --short
```

The second command must be empty before the final rebuild is accepted as code-frozen.

## 2. Run non-destructive code/readiness checks

Compile the ETL package and run tests available in the repository:

```bash
python -m compileall -q src/pcornet_omop_validation/etl
pytest
```

Run the standard preflight and mapping-semantics checks:

```bash
pcornet-omop-etl preflight --config config/etl_A.yaml
python -m pcornet_omop_validation.etl.mapping_semantics_preflight \
  --config config/etl_A.yaml
```

Before resetting the currently populated validated target, exercise the Person existing-target validation path. This is read-only with respect to OMOP rows and verifies the idempotence robustness fix that was not exercised by an empty-target build:

```bash
python - <<'PY'
from pcornet_omop_validation.etl.config import load_etl_config
from pcornet_omop_validation.etl.person import transform_person

result = transform_person(load_etl_config("config/etl_A.yaml"))
print("status:", result.status)
print("target_rows:", result.target_rows)
PY
```

Expected status on the current matched populated target:

```text
already_loaded_matched
```

Then run the read-only rebuild readiness audit:

```bash
python -m pcornet_omop_validation.etl.rebuild_readiness \
  --config config/etl_A.yaml
```

On a populated target, `ready_for_guarded_reset` is expected if there are no blockers.

## 3. Guarded reset of the isolated validated target

Dry-run first:

```bash
python -m pcornet_omop_validation.etl.clean_reset \
  --config config/etl_A.yaml
```

Review the reported database/schema and incoming foreign-key checks. The reset path is allowed to remove only derived core OMOP rows and `etl_*` objects in the configured validated target schema. It must preserve staging tables, vocabulary tables, OMOP DDL, and every other database.

Execute only after confirming the dry-run points to the isolated validated target:

```bash
python -m pcornet_omop_validation.etl.clean_reset \
  --config config/etl_A.yaml \
  --execute \
  --confirm-database OMOP_VALIDATED_A \
  --confirm-schema dbo
```

After reset, run readiness again and require `ready_for_clean_build`, empty core targets, populated vocabulary, and detected PCORnet staging tables.

## 4. Final clean rebuild

Run the clean-build phases in dependency order:

```text
01  clean_build_phase1
02  clean_build_phase2_routes
03  clean_build_phase3_primary_events
04  clean_build_phase4_measurement_base
05  clean_build_phase5_measurement_obsclin
06  clean_build_phase6_observation
07  clean_build_phase7_condition_obsclin
08  clean_build_phase8_drug
09  clean_build_phase9_procedure_remaining
10  clean_build_phase10_condition_cross_domain
11  clean_build_phase11_death
12  clean_build_phase12_validation
13  clean_build_phase13_review_decisions
14  clean_build_phase14_freeze_manifest
```

Run each phase with:

```bash
python -m pcornet_omop_validation.etl.<module_name> \
  --config config/etl_A.yaml
```

Do not rerun route builders independently after downstream target/xwalk materialization unless deliberately restarting that portion of the clean build. In particular, the Drug route builder recreates its route ledger.

## 5. Final acceptance criteria

The final publication build is accepted on semantic and reconciliation properties, not on reproducing historical row counts.

Require all of the following:

- all materialization phase reconciliation statuses are matched;
- visit-time semantics are matched and materialized datetime mismatch rows are zero;
- global source/route/lineage reconciliation is matched;
- duplicate primary-key groups are zero;
- audited reversed intervals are zero;
- semantic hard blockers are empty;
- every semantic review flag is explicitly explained;
- unexplained review flags are empty;
- auxiliary concept blockers are empty;
- the Phase 14 manifest records the intended Git commit;
- the Phase 14 manifest records zero dirty-worktree entries.

Concept-zero counts and mapping coverage are reported outcomes and review quantities. They are not failures by themselves when they follow the explicit fail-closed provenance/mapping policy.

## 6. Archive the reproducibility bundle

Preserve the final Phase 1-14 audit JSON files together with:

- exact Git commit SHA;
- publication configuration hash/path with secrets excluded from sharing;
- ETL source SHA-256 manifest;
- source/vocabulary provenance and hashes where permissible;
- route and lineage reconciliation summaries;
- quantified exclusions and ambiguity policies;
- semantic review decisions;
- the final freeze manifest.

Do not commit protected source data, credentials, Athena packages, or sensitive result bundles to the public repository.

## 7. Transition to publication analyses

After the final freeze is complete, do not change ETL semantics for the purpose of improving agreement with the comparator. Any later ETL defect must be documented, fixed generally, and followed by another clean freeze rebuild.

The scientific analysis then proceeds through:

1. structural and semantic concordance;
2. patient-level concordance;
3. phenotype reproducibility;
4. analytical equivalence and sensitivity analysis;
5. manuscript tables, figures, Methods, Results, and reproducibility materials.

The goal of the frozen ETL is to make observed PCORnet-versus-OMOP differences interpretable as representation/model effects rather than uncontrolled converter behavior.
