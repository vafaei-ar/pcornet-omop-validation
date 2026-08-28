# Stage B Wave 2 analytical lock record

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Locked Wave 2 definition: `study_definitions/stage_b_wave2_v1.json`

Status: **analytically complete**

## Completion criteria

The final Wave 2 manuscript/invariant bundle completed successfully with:

- `all_invariants_matched: true`
- `disclosure_review.status: passed`
- no source-unmatched mapped Drug rows
- no source-unmatched mapped Measurement/Observation rows
- no target-unmatched Measurement/Observation rows in the prespecified semantic space
- Drug target excess fully attributed to other audited provenance
- all uniquely resolved active Standard UCUM units agreeing exactly
- all prespecified mapped categorical value concepts agreeing exactly
- VITAL direct-source numeric differences fully explained by the frozen ETL SQL expression, with zero unexplained target mismatches

## Primary Wave 2 results

| Semantic family | Source mapped rows | Exact matched | Source unmatched | Target rows in source concept space | Target excess before attribution | Other provenance | Unresolved / concept zero | Patient Jaccard |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Drug | 30,988,400 | 30,988,400 | 0 | 30,988,448 | 48 | 48 | 17,469,480 | 1.0 |
| Measurement/Observation | 92,668,145 | 92,668,145 | 0 | 92,668,145 | 0 | 0 | 366,371 | 1.0 |

## Secondary value/unit results

- Numeric directly comparable rows: 75,769,622.
- Direct-source exact numeric matches: 75,644,000; 125,622 direct-source differences were all VITAL rows.
- Reproduction of the frozen VITAL `CROSS APPLY (VALUES ...)` expression yielded 0 target mismatches and 0 unexplained numeric differences.
- Rows with unit semantics: 82,054,878.
- Uniquely resolved active Standard UCUM rows: 58,916,347; exact agreement among resolved units: 100%; unresolved UCUM rows: 23,138,531.
- Categorical VITAL rows: 2,170,885.
- Prespecified mapped Standard categorical value rows: 809,630; exact mapped agreement: 100%; concept-zero policy rows: 1,361,255.

## Disclosure review

The final manuscript bundle is aggregate-only and records:

- no patient identifiers written
- no source-record identifiers written
- no row-level PHI written
- no free-text clinical values written

## Interpretation policy

Coverage limitations such as Drug concept zero, unresolved UCUM units, and categorical value concept zero remain reported as coverage results under the frozen policy; they are not retroactively converted into concordance failures. No ETL mapping was changed based on observed Wave 2 outcomes. The frozen ETL SHA remains immutable.

## Reproducibility outputs

The final local bundle is produced by:

`python -m pcornet_omop_validation.study.stage_b_wave2_manuscript_tables --config config/etl_A.yaml`

Expected aggregate outputs include:

- `results/publication_analysis/stage_b_patient_concordance/stage_b_wave2_manuscript_primary.csv`
- `results/publication_analysis/stage_b_patient_concordance/stage_b_wave2_manuscript_value_layers.csv`
- `results/publication_analysis/stage_b_patient_concordance/stage_b_wave2_vital_numeric_representation.csv`
- `results/publication_analysis/stage_b_patient_concordance/stage_b_wave2_final_summary.json`
- `results/publication_analysis/stage_b_patient_concordance/stage_b_wave2_manuscript_tables.md`

This record closes Stage B Wave 2 analysis without modifying the frozen ETL.
