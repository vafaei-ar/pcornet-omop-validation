# Stage C stroke D1/D3 post-outcome index-date selection audit

This diagnostic analysis is intentionally post-outcome. It was designed after the completed prespecified D1/D3 concordance showed approximately 97.4% to 97.5% exact shared index-date agreement with a small set of large day differences.

The audit does not modify the frozen ETL, locked phenotype definitions, code lists, evidence windows, age rule, cohort ordering rule, or completed primary concordance outputs.

For shared D1/D3 patients with different selected index dates, the audit separates:

1. same-encounter index-date representation differences;
2. different-episode selection where the PCORnet-selected stroke diagnosis lacks OMOP lineage and has null DX_DATE;
3. other missing-lineage cases;
4. source-selected episodes that materialize in OMOP but fail target imaging/lipid/date/age qualification;
5. residual ordering differences where both episodes remain target-qualifying.

Only aggregate counts are written. The audit must reproduce the already completed D1/D3 shared and exact-date counts before its diagnostic categories are accepted.
