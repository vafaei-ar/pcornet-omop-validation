# Stage C D1/D3 post-outcome mechanism audit

The primary D1/D3 concordance completed before this audit was designed. The completed result is retained unchanged and remains the prespecified primary transformation-fidelity analysis.

This follow-up audit is explicitly post-outcome and explanatory. It re-materializes only the locked PCORnet source-reference D1/D3 cohorts and the lineage-faithful frozen OMOP D1/D3 cohorts. It does not rerun or redefine the secondary native-OMOP portability analysis.

The audit tests whether primary source-only patients have null selected qualifying `DX_DATE`, whether their qualifying diagnosis has frozen ETL lineage in `etl_condition_occurrence_xwalk`, and whether the corresponding lineage-faithful OMOP base event exists. It also reports the aggregate day-difference distribution for shared source/OMOP index dates.

The audit must reproduce the already completed source, OMOP, shared, and source-only cohort counts before its mechanism results are accepted. No ETL rule, phenotype definition, code list, date window, age rule, cohort ordering rule, or frozen artifact is modified.

Because this diagnostic was designed after observing D1/D3 concordance, it may be used to explain the mechanism of discordance but must not be presented as a prespecified confirmatory analysis.

All outputs are aggregate only. No patient-level or source-record-level identifiers are written.
