# Stage C stroke D1/D3 performance note

The first Stage C D1/D3 concordance execution was manually interrupted after approximately two hours while it was at the native-OMOP portability materialization stage. No `stage_c_stroke_d1_d3_concordance.json` output had been written, so no D1/D3 concordance outcome was observed before the performance intervention.

The stalled query preserved the prespecified D1/D3 phenotype but evaluated native OMOP imaging and lipid evidence through correlated `EXISTS` predicates over large OMOP fact tables. The intervention therefore changes physical query support only. It does not change the frozen ETL, source-reference phenotype, standard concept sets, imaging or lipid windows, age rule, event ordering, lineage-faithful primary comparison, or native-OMOP portability definition.

`pcornet_omop_validation.study.stage_c_stroke_d1_d3_index_prep` creates four nonclustered analysis indexes:

- `condition_occurrence(condition_concept_id, visit_occurrence_id, person_id)`
- `procedure_occurrence(person_id, procedure_date, procedure_concept_id)`
- `measurement(person_id, measurement_date, measurement_concept_id)`
- `observation(person_id, observation_date, observation_concept_id)`

Indexes are created one at a time and committed separately so the preparation is resumable. Re-running the command is idempotent by index name. These indexes alter no table rows and are not ETL transformations.

After adding this performance-only code, rerun the outcome-free D1/D3 preflight before concordance so the preflight records the new analysis Git SHA. Then run the unchanged D1/D3 concordance module.
