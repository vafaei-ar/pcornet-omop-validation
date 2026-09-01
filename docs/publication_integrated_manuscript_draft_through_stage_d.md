# Integrated manuscript draft through Stage D

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This document supersedes the prior manuscript draft through Stage C by integrating the completed Stage D analytical-equivalence analysis. It preserves the staged interpretation: structural transformation, mapped semantic preservation, phenotype reproducibility, and downstream analytical equivalence are distinct estimands.

## Working title

**From PCORnet to OMOP: separating transformation fidelity, semantic preservation, phenotype reproducibility, and analytical equivalence in an audited common-data-model conversion**

## Study objective

We evaluated whether clinical information represented in PCORnet was preserved after conversion to OMOP CDM v5.4.2. We separated four questions: whether the ETL followed explicit transformation rules; whether mapped patient-level clinical semantics were preserved; whether complete computable phenotypes selected the same patients; and whether downstream scientific estimates were analytically equivalent.

# Methods

## Study design and analysis freeze

The publication ETL was frozen at Git commit `887e6f4d60a6b185e58b3c9fe8887472b49777e3` before downstream patient-level validation. Publication analyses were executed separately on `publication/analysis`. Downstream discordance findings were not used to retune the frozen ETL.

The ETL targeted OMOP CDM v5.4.2 and used a frozen Athena vocabulary. Records missing required target dates were excluded rather than assigned artificial sentinel dates. Source codes without a defensible unique active Standard mapping were retained with concept `0` when permitted by the target representation. One-to-many Standard mappings and cross-domain routes were preserved. Patient linkage used PCORnet `PATID` to OMOP `person_source_value`, with uniqueness required.

## Stage A: structural transformation

Stage A quantified source eligibility, explicit exclusions, route generation, vocabulary coverage, cross-domain transformation, one-to-many expansion, concept-zero retention, and final OMOP target counts. Raw source-to-target row-count equality was not treated as a general fidelity criterion because one source record may validly yield multiple OMOP semantic routes or route to another OMOP domain.

## Stage B: patient-level semantic concordance

Stage B evaluated native OMOP preservation of mapped Encounter, Death, Condition, Procedure, Drug, Measurement, and Observation semantics. Primary identities used patient, clinically appropriate date, target domain, and Standard concept. Concept-zero rows and unresolved units/values were reported as coverage limitations rather than included in mapped semantic agreement denominators. Secondary analyses examined numeric values, UCUM units, categorical values, and target-side excess provenance.

## Stage C: ischemic-stroke phenotype reproducibility

Stage C evaluated locked D0, D1, and D3 ischemic-stroke phenotypes. The primary transformation-fidelity estimand used lineage-faithful OMOP so source-only semantics such as PCORnet `PDX` could be represented through frozen lineage. A secondary native-OMOP portability estimand used active Standard concepts without reconstructing `PDX`.

D0 selected the earliest qualifying adult inpatient/emergency-to-inpatient ischemic-stroke index encounter. D1 added CT or MRI imaging plus lipid testing. D3 added MRI plus lipid testing. Post-outcome mechanism audits were explanatory only and did not change the locked phenotype definitions.

## Stage D: downstream analytical equivalence

Stage D inherited the exact locked Stage C D0 index definition. Outcomes and equivalence margins were frozen before the first cross-CDM outcome query.

The primary endpoint was any ED, emergency-to-inpatient, or inpatient acute-care encounter/visit beginning after index discharge and within 90 days, inclusive, with representation-specific continuous observability through day 90. The secondary endpoint used the same event definition through day 30. An exploratory recurrent ischemic-stroke outcome required an acute-care encounter/visit plus the locked stroke diagnosis semantics during days 31 through 365.

The primary fixed-index estimand restricted to patients shared by source D0 and lineage-faithful OMOP D0 with the same selected index date and follow-up observability in both representations. This holds cohort membership and index date fixed so differences primarily reflect post-index event representation.

The secondary end-to-end estimand independently used the source D0 and lineage-faithful OMOP D0 cohorts. This intentionally allows upstream phenotype attrition to propagate into the downstream estimate.

Prespecified equivalence required both an absolute risk difference within ±0.5 percentage points and an OMOP/source risk ratio between 0.95 and 1.05. A nonsignificant difference was not treated as evidence of equivalence.

A post-outcome recurrent-stroke mechanism diagnostic was conducted only after the exploratory discordance was observed. It reproduced the locked recurrent endpoint and decomposed discordance into visit lineage, condition lineage, temporal-window, and boundary mechanisms without writing patient identifiers.

## Reproducibility and disclosure

Final manuscript-oriented artifacts record the frozen ETL SHA, study-definition hashes, analysis-code SHA, and aggregate results. Patient identifiers and row-level protected health information are not written to committed outputs.

# Results

## Stage A: explicit transformation rules explain structural differences

The frozen OMOP build contained 27,089 persons, 27,087 observation periods, 1,510,957 visits, 7,315,572 condition occurrences, 4,182,803 procedure occurrences, 85,715,435 measurements, 7,319,081 observations, 48,458,058 drug exposures, 196,660 device exposures, 93 specimens, and 6,955 deaths.

Required-date exclusions were concentrated in DIAGNOSIS and PROCEDURES. Of 11,484,577 DIAGNOSIS rows, 3,459,785 lacked `DX_DATE` and were excluded. Of 11,244,947 PROCEDURES rows, 16,924 lacked `PX_DATE` and were excluded. One-to-many and cross-domain routing produced expected differences between eligible source-event counts and OMOP target-row counts. Drug mapping coverage remained incomplete, with 17,469,480 concept-zero Drug routes.

## Stage B: mapped semantics were preserved exactly

Encounter and Death were exactly concordant. Every mapped source Condition, Procedure, Drug, and Measurement/Observation semantic route in the prespecified denominators was present in native OMOP. Target-side excess for Condition and Procedure was fully explained by other audited PCORnet source provenance. Resolved UCUM units and prespecified categorical value concepts agreed exactly. All initially observed VITAL numeric discrepancies were explained by the frozen ETL SQL expression, leaving zero unexplained target numeric mismatches.

## Stage C: phenotype reproducibility was limited by diagnosis-date eligibility

The source D0 cohort contained 9,815 patients and the lineage-faithful OMOP D0 cohort contained 6,001. All 6,001 shared patients had the same index date. There were 3,814 source-only patients and no OMOP-only patients.

For D1, PCORnet contained 8,624 patients and lineage-faithful OMOP contained 5,246; 5,245 were shared, 3,379 were source-only, and 1 was OMOP-only. Patient Jaccard was 0.608. Among shared patients, 97.39% had the same selected index date.

For D3, PCORnet contained 7,565 patients and lineage-faithful OMOP contained 4,710; 4,709 were shared, 2,856 were source-only, and 1 was OMOP-only. Patient Jaccard was 0.622. Among shared patients, 97.52% had the same selected index date.

Post-outcome audits showed that every D1/D3 source-only patient had a null selected `DX_DATE` and no diagnosis lineage under the frozen required-date policy. Every residual shared-patient index-date difference was explained by selection of another qualifying episode after loss of the source-selected diagnosis. Imaging and lipid transformation were not the dominant source of phenotype discordance.

## Stage D: fixed-index outcomes were exactly reproduced, but end-to-end equivalence failed

Stage D reproduced the D0 anchors: 9,815 source D0 patients, 6,001 lineage-faithful OMOP D0 patients, and 6,001 shared patients with exact index dates.

### Primary fixed-index 90-day outcome

Among 3,822 patients observable through 90 days in both representations, PCORnet and OMOP each identified 1,132 acute-care events. Risk was 29.6180% in both representations. The absolute risk difference was 0.0000 percentage points and the OMOP/source risk ratio was 1.0000. There were no source-only or OMOP-only positive labels. Both prespecified equivalence margins were met.

Among all 1,132 patients positive in both representations, first-event dates matched exactly for all 1,132 and were within one day for all 1,132.

### Secondary fixed-index 30-day outcome

Among 4,374 patients observable through 30 days in both representations, PCORnet and OMOP each identified 753 events. Risk was 17.2154% in both. The absolute risk difference was 0.0000 percentage points and the risk ratio was 1.0000. There were no discordant positive labels, and both equivalence margins were met.

### End-to-end 90-day analysis

The independently selected source cohort included 6,508 eligible patients with 1,798 events and risk 27.6275%. The lineage-faithful OMOP cohort included 3,822 eligible patients with 1,132 events and risk 29.6180%. The absolute risk difference was +1.9905 percentage points and the OMOP/source risk ratio was 1.0720. Neither the absolute nor relative equivalence margin was met.

### End-to-end 30-day analysis

The independently selected source cohort included 7,277 eligible patients with 1,178 events and risk 16.1880%. OMOP included 4,374 eligible patients with 753 events and risk 17.2154%. The absolute risk difference was +1.0274 percentage points and the risk ratio was 1.0635. Neither equivalence margin was met.

The contrast between exact fixed-index equivalence and failed end-to-end equivalence localizes the analytical divergence upstream to cohort construction rather than post-index acute-care outcome transformation.

### Exploratory recurrent ischemic stroke

Among 2,531 fixed-index patients observable through 365 days, PCORnet identified 263 recurrent ischemic-stroke events and OMOP identified 258. Labels agreed for 2,526 patients, or 99.80%. Five patients were source-only positive and none were OMOP-only positive.

The post-outcome mechanism diagnostic showed that all five discordant patients retained the encounter-to-visit crosswalk, an OMOP visit, an acute-care OMOP visit concept, and a visit within the locked day-31 through day-365 window. All five lacked a DIAGNOSIS-to-condition crosswalk for the qualifying recurrent stroke diagnosis and therefore lacked a linked OMOP condition. None occurred at the day-31 or day-365 boundary. Their source recurrent events occurred between days 60 and 345. Index discharge dates were exactly concordant for all 2,531 eligible patients.

Across 2,144 source recurrent-stroke candidate diagnosis rows, all 2,144 retained visit lineage and temporal qualification, whereas 1,978 retained diagnosis-to-condition lineage. Thus, recurrent-stroke discordance arose from diagnosis materialization/condition-lineage loss rather than encounter representation or temporal-window drift.

# Integrated interpretation

The results show that common-data-model validation should not be reduced to one global concordance metric.

First, structural differences can be legitimate consequences of explicit ETL eligibility, vocabulary mapping, one-to-many routing, and cross-domain representation.

Second, mapped patient-level clinical semantics can be preserved exactly even when vocabulary coverage is incomplete.

Third, complete phenotype reproducibility can be substantially lower because source-model semantics and ETL eligibility rules determine which patients remain available for cohort construction.

Fourth, downstream analytical results depend on which of these layers is held fixed. In Stage D, post-index acute-care outcome representation was exact when the same patients and index dates were compared. However, end-to-end equivalence failed because upstream D0 cohort attrition changed the population entering the analysis. Therefore, an apparently different downstream risk estimate can arise without any loss of the outcome event itself.

This distinction is the main scientific contribution of the staged design. The transformation preserved mapped events and fixed-index outcomes extremely well, while a specific required-date policy materially affected phenotype membership and therefore end-to-end analytical reproducibility.

# Main Stage D table

| Estimand | PCORnet eligible | OMOP eligible | PCORnet events | OMOP events | PCORnet risk | OMOP risk | Absolute difference, pp | Risk ratio | Equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Fixed-index 90 d | 3,822 | 3,822 | 1,132 | 1,132 | 29.618% | 29.618% | 0.000 | 1.000 | Met |
| Fixed-index 30 d | 4,374 | 4,374 | 753 | 753 | 17.215% | 17.215% | 0.000 | 1.000 | Met |
| End-to-end 90 d | 6,508 | 3,822 | 1,798 | 1,132 | 27.628% | 29.618% | +1.990 | 1.072 | Not met |
| End-to-end 30 d | 7,277 | 4,374 | 1,178 | 753 | 16.188% | 17.215% | +1.027 | 1.063 | Not met |

# Limitations

1. The study evaluates one source dataset and one audited PCORnet-to-OMOP implementation. External generalizability remains unknown.
2. Native OMOP cannot reproduce all source-model semantics, including PCORnet `PDX`, without lineage or additional representation.
3. Vocabulary coverage remains incomplete for some drug codes, units, categorical values, and a subset of locked lipid LOINCs.
4. Stage C mechanism audits and the recurrent-stroke Stage D diagnostic were designed after observing the primary results and are explanatory, not prespecified confirmatory analyses.
5. Stage D demonstrates analytical equivalence for prespecified acute-care risk endpoints conditional on a fixed index cohort. It does not establish universal equivalence for all future outcomes, exposures, models, or effect estimates.
6. End-to-end risk differences should not be interpreted as an intrinsic OMOP CDM effect. They reflect this specific interaction between source phenotype semantics and the frozen ETL eligibility policy.

# Suggested abstract results paragraph through Stage D

In structural validation, the main explicit source exclusions were 3,459,785 diagnoses missing `DX_DATE` and 16,924 procedures missing `PX_DATE`; one-to-many and cross-domain vocabulary routes were preserved rather than forced into one-to-one row correspondence. All mapped clinical semantic events in the prespecified Stage B denominators were found in OMOP, with no unexplained residual discordance after provenance and value-expression audits. Ischemic-stroke phenotype Jaccard was 0.611 for D0, 0.608 for D1, and 0.622 for D3; all D1/D3 source-only patients were explained by diagnosis records with null `DX_DATE` that the source phenotype could use through encounter-date fallback but the frozen ETL excluded. In Stage D, fixed-index 90-day and 30-day acute-care risks were identical between PCORnet and OMOP, with risk ratios of 1.000 and absolute differences of 0.000 percentage points, meeting both prespecified equivalence margins. In contrast, end-to-end equivalence failed at 90 and 30 days because the earlier phenotype attrition changed the eligible OMOP cohort. Exploratory recurrent-stroke labels agreed in 99.80% of fixed-index patients; all five source-only recurrent events lacked diagnosis-to-condition lineage while retaining visit lineage and correct temporal placement.

# Suggested conclusion through Stage D

An audited PCORnet-to-OMOP conversion can preserve mapped clinical semantics and downstream outcome events exactly while still altering complete phenotype cohorts and end-to-end analytical estimates. In this dataset, the dominant divergence arose from a required diagnosis-date eligibility rule rather than from post-index acute-care event transformation. Validation of common-data-model conversion should therefore separate structural transformation, semantic preservation, phenotype portability, and conditional versus end-to-end analytical equivalence rather than report a single global concordance measure.
