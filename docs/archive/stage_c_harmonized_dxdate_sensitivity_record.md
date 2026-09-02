# Stage C harmonized nonmissing-DX_DATE sensitivity

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This is a post-freeze sensitivity analysis. It does not replace or redefine the locked source-faithful Stage C D0/D1/D3 analyses.

## Question

The primary source-reference phenotype allowed the locked PCORnet index-date fallback `COALESCE(DX_DATE, ADMIT_DATE, DISCHARGE_DATE)`, whereas the frozen ETL excluded DIAGNOSIS records with missing `DX_DATE`. This sensitivity asks whether any phenotype discordance remains after requiring nonmissing `DX_DATE` symmetrically in the source phenotype before comparing with lineage-faithful OMOP.

## Results

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | Source only | OMOP only | Patient Jaccard | Exact shared index date |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 6,198 | 6,198 | 6,198 | 0 | 0 | 1.000 | 100.0% |
| D1 | 5,246 | 5,246 | 5,246 | 0 | 0 | 1.000 | 100.0% |
| D3 | 4,710 | 4,710 | 4,710 | 0 | 0 | 1.000 | 100.0% |

The selected source lipid date field remained `SPECIMEN_DATE`.

## Interpretation

Under symmetric diagnosis-date eligibility, D0, D1, and D3 were reproduced exactly at both the patient and selected-index-date levels. Therefore, the lower concordance in the primary source-faithful Stage C comparison is attributable to the interaction between the PCORnet source phenotype's encounter-date fallback and the frozen ETL's required nonmissing `DX_DATE` rule, rather than an inability of the lineage-faithful OMOP representation to reproduce the phenotype once eligibility is harmonized.

This sensitivity strengthens, rather than replaces, the primary result. The primary comparison estimates practical source-to-target phenotype reproducibility under the locked source semantics and ETL policy. The harmonized sensitivity isolates residual representation discordance after equalizing the diagnosis-date eligibility rule; none remained in this dataset.

## Reporting guardrail

Do not describe the harmonized result as evidence that PCORnet and OMOP phenotypes are universally identical. It demonstrates exact reproduction for these locked D0/D1/D3 definitions after imposing the same nonmissing-`DX_DATE` eligibility rule in this dataset and frozen ETL implementation.

## Disclosure

The sensitivity output is aggregate only. No patient identifiers, source-record identifiers, or row-level protected health information are committed.
