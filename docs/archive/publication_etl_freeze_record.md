# Publication ETL freeze record

## Freeze identity

- Frozen ETL commit: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`
- Freeze date: 2026-08-27
- Validated target: `OMOP_VALIDATED_A`
- Source schema: `dbo`
- Target schema: `dbo`
- Final manifest: `results/etl_audit_A/clean_build_phase14_freeze_manifest.json`
- Final manifest status: `freeze_candidate_manifested`
- Final manifest dirty worktree entries: `0`

The comparator/prior OMOP database was not reset, truncated, rebuilt, or otherwise modified by the validated-target reset/rebuild path.

## Freeze acceptance criteria

The publication ETL freeze was accepted because the final clean rebuild satisfied all of the following:

- global source/route/lineage reconciliation matched;
- visit-time semantics matched;
- materialized visit datetime mismatches were zero;
- duplicate primary-key groups were zero;
- reversed audited intervals were zero;
- semantic hard blockers were empty;
- all semantic review flags were explicitly explained;
- unexplained semantic review flags were empty;
- auxiliary concept blockers were empty;
- the working tree was clean when the final manifest was generated.

Row counts were not used as acceptance thresholds. They are reported as reproducibility outputs of the frozen source/vocabulary/ETL combination.

## Final target counts

| OMOP table | Rows |
| --- | ---: |
| person | 27,089 |
| observation_period | 27,087 |
| visit_occurrence | 1,510,957 |
| condition_occurrence | 7,315,572 |
| procedure_occurrence | 4,182,803 |
| measurement | 85,715,435 |
| observation | 7,319,081 |
| drug_exposure | 48,458,058 |
| device_exposure | 196,660 |
| specimen | 93 |
| death | 6,955 |

## Final validation summary

Phase 12:

- `visit_time_semantics_status: matched`
- `materialized_datetime_mismatch_rows: 0`
- `global_reconciliation_status: matched`
- `semantic_hard_blockers: []`
- 10 semantic review flags

Phase 13:

- `status: freeze_candidate_reviewed`
- `semantic_review_flag_count: 10`
- `explained_review_flag_count: 10`
- `unexplained_review_flags: []`

Phase 14:

- `phase13_status: freeze_candidate_reviewed`
- `visit_time_semantics_status: matched`
- `auxiliary_concept_blockers: []`
- `git_head: 887e6f4d60a6b185e58b3c9fe8887472b49777e3`
- `dirty_worktree_entries: 0`
- `etl_source_files_hashed: 82`
- `audit_json_files_hashed: 66`

## Explicit semantic review quantities

The final build retained explicit concept-0/provenance quantities rather than forcing unsupported semantics. The final review included:

- Condition type concept 0: 592,727 rows
- Procedure type concept 0: 4,182,803 rows
- Measurement type concept 0: 85,715,435 rows
- Observation type concept 0: 7,319,081 rows
- Drug Exposure type concept 0: 178 rows
- Device Exposure type concept 0: 196,660 rows
- Specimen type concept 0: 93 rows
- Death type concept 0: 6,955 rows
- Drug Exposure nonblank route source with route concept 0: 28,378,437 rows
- Death rows with `death_type_concept_id=0` under explicit provenance policy: 6,955 rows

These were reviewed as transparent semantic limitations/policies, not reconciliation failures.

## Rebuild sequence

The frozen build was produced only after:

1. code cleanup and commit;
2. direct re-test of the Person existing-target idempotence path;
3. clean Python compilation;
4. guarded reset of the validated target only;
5. readiness confirmation with all core targets empty and no blockers;
6. complete Phase 1-14 rebuild without tracked source edits during execution;
7. final manifest generation with a clean worktree.

## Freeze policy for publication analyses

The ETL freeze is treated as immutable during downstream scientific comparison. Publication-analysis code may evolve on a separate branch, but the ETL must not be changed merely to improve PCORnet-versus-OMOP agreement. If a new ETL defect is discovered, it must be independently justified, documented, corrected prospectively, and followed by a new clean rebuild/freeze record before analyses are repeated.
