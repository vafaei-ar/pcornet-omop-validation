# Stage D analytical-equivalence lock record

Stage D is defined before any cross-CDM post-stroke outcome query.

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Study definition: `study_definitions/stage_d_stroke_analytical_equivalence_v1.json`

## Locked index cohort

Stage D inherits the exact Stage C D0 source and lineage-faithful OMOP definitions. No diagnosis code, PDX, encounter, overnight, age, ordering, patient-linkage, or index-date rule is redefined for Stage D.

The authoritative Stage C D0 overnight rule is a calendar overnight rule (`DISCHARGE_DATE - ADMIT_DATE >= 1 calendar day`), not the earlier planning-stage >24-hour timestamp variant.

## Locked outcomes

Primary outcome: any ED, emergency-to-inpatient, or inpatient acute-care encounter/visit beginning after index discharge and within 90 days, inclusive, with representation-specific continuous observability through day 90.

Secondary outcome: the same acute-care definition through 30 days.

Exploratory epidemiologic outcome: acute-care recurrent ischemic stroke during days 31-365, requiring a qualifying acute-care encounter/visit plus the locked ischemic-stroke diagnosis semantics. This is not a prediction endpoint.

## Locked estimands

1. **Primary fixed-index outcome fidelity.** Restrict to patients shared by source D0 and lineage-faithful OMOP D0 with the same index date and observable follow-up in both representations. This isolates post-index outcome-event transformation as much as possible.
2. **Secondary end-to-end analytical reproducibility.** Estimate outcome risk independently in source D0 and lineage-faithful OMOP D0. This intentionally allows Stage C phenotype attrition to affect the downstream scientific estimate.
3. **Secondary time-to-event agreement.** Among fixed-index patients positive in both representations, compare first acute-care event dates.

## Prospective equivalence margins

For 90-day and 30-day outcome risk:

- absolute risk difference: within ±0.5 percentage points;
- relative risk ratio (OMOP / PCORnet): 0.95-1.05.

A nonsignificant difference is not evidence of equivalence. Absolute and relative criteria are reported separately and jointly.

The historical protocol also specified a general effect-estimate ratio margin of 0.90-1.10 for later regression/effect-estimation analyses. Stage D v1 does not introduce a new exposure-effect model solely to fill that slot; the primary scientific estimand is post-stroke acute-care risk.

## Guardrails

- The outcome-free preflight must run before the Stage D outcome module.
- Outcome definitions and equivalence margins may not be changed after cross-CDM results are observed.
- Stage C post-outcome mechanism audits remain explanatory and do not redefine the Stage D index cohort.
- Stage D findings do not reopen the frozen ETL unless an independent ETL defect is demonstrated.
- Patient-level outputs remain outside Git. Manuscript artifacts are aggregate and disclosure-reviewed.
