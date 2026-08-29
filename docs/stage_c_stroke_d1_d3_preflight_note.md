# Stage C D1/D3 preflight vocabulary coverage note

The first local D1/D3 preflight run on 2026-08-28 stopped because the preflight incorrectly required every locked lipid LOINC to resolve to an active source concept and an allowed active Standard OMOP target.

Observed frozen-vocabulary coverage from that run was:

- locked lipid LOINCs: 214
- active source concepts: 194
- active Standard Measurement/Observation targets: 192
- unresolved for native portability: 22
- Standard target domains: 187 Measurement codes and 5 Observation codes

This was a preflight-policy defect, not a phenotype or ETL defect. The locked source-reference phenotype defines lipid evidence by exact membership in the versioned PROMIS LOINC artifact. Therefore frozen-vocabulary absence or deprecation cannot remove a locked source code from the primary source-reference phenotype. For the primary transformation-fidelity estimand, exact source identity plus frozen lineage remains authoritative. For the secondary native-OMOP portability sensitivity, only the active Standard-resolved subset is natively representable; unresolved/deprecated codes are reported as coverage limitations and are not silently remapped.

The corrected preflight keeps full resolution of the locked imaging CPT set as a hard prerequisite, but treats incomplete lipid vocabulary resolution as an explicit coverage result rather than a failure. No D1/D3 outcome query had been performed before this correction.
