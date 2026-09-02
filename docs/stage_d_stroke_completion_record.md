# Stage D ischemic-stroke analytical-equivalence completion record

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Study definition: `study_definitions/stage_d_stroke_analytical_equivalence_v1.json`

Stage D is analytically complete under the prespecified fixed-index and end-to-end estimands. A protocol-completion supplement subsequently reported the prespecified median time-to-first-event metric that had been omitted from the original completed JSON, and a clearly labeled post-outcome recurrent-stroke `PDX=P` sensitivity resolved an ambiguity in the exploratory wording without altering the frozen primary or secondary estimands.

## Provenance

- Outcome-free preflight analysis SHA: `6e6d2139ab09648b5c59770788ce7762ed5ffb7d`
- Completed Stage D analysis SHA: `1e4508ff55928965249beb843646232f3a234048`
- Recurrent-stroke discordance diagnostic SHA: `6321336996cd826dfade84509a81691a8aaaf260`
- Protocol-completion supplement SHA: `c97a3e65880b58fb39cf66c548164d46c61fbc5d`
- Study-definition SHA-256: `e8a0914bee9993f34167ad2b336191f2a0c52e88e47adb99c9f2c94cd6f7abbf`
- Inherited D0-definition SHA-256: `45c4fe08493e6a1713b2d1b38b6a6903e67784a1edcb5d01de119cccc3489418`

The difference between the preflight and completed-analysis SHAs reflects a SQL Server compatibility fix to the date-concordance aggregation expression. The locked cohort, outcome, observability, and equivalence definitions did not change. The later supplement did not change any frozen definition; it completed one prespecified reporting metric and added one post-outcome sensitivity analysis.

## D0 reproduction

| Representation | Patients |
| --- | ---: |
| PCORnet source D0 | 9,815 |
| Lineage-faithful OMOP D0 | 6,001 |
| Shared with exact index date | 6,001 |

Stage D reproduced the locked Stage C D0 anchors exactly.

## Primary fixed-index 90-day outcome fidelity

Among patients shared by source and lineage-faithful OMOP D0 with identical selected index dates and 90-day observability in both representations:

| Metric | PCORnet | OMOP |
| --- | ---: | ---: |
| Eligible | 3,822 | 3,822 |
| Events | 1,132 | 1,132 |
| Risk | 29.6180% | 29.6180% |
| Median days to first event | 26.0 | 26.0 |

- Absolute risk difference: `0.0000` percentage points.
- Relative risk ratio, OMOP / PCORnet: `1.0000`.
- Source-only positives: `0`.
- OMOP-only positives: `0`.
- Both prespecified equivalence margins were met.
- Among all 1,132 patients positive in both representations, the first-event date matched exactly in 1,132 and within one day in 1,132.

The median time-to-first-event metric was prespecified in the Stage D definition but omitted from the original completed JSON. The aggregate-only completion supplement reproduced the locked denominator and event counts and reported an identical median of 26.0 days in both representations.

This is exact fixed-index outcome-label, event-date, and median-time reproduction for the primary Stage D endpoint.

## Secondary fixed-index 30-day outcome fidelity

| Metric | PCORnet | OMOP |
| --- | ---: | ---: |
| Eligible | 4,374 | 4,374 |
| Events | 753 | 753 |
| Risk | 17.2154% | 17.2154% |

- Absolute risk difference: `0.0000` percentage points.
- Relative risk ratio, OMOP / PCORnet: `1.0000`.
- Source-only positives: `0`.
- OMOP-only positives: `0`.
- Both prespecified equivalence margins were met.

## Secondary end-to-end analytical reproducibility

End-to-end comparisons intentionally allow the previously observed D0 phenotype attrition to propagate into downstream estimates.

### 90 days

| Metric | PCORnet | OMOP |
| --- | ---: | ---: |
| Eligible | 6,508 | 3,822 |
| Events | 1,798 | 1,132 |
| Risk | 27.6275% | 29.6180% |

- Absolute risk difference: `+1.9905` percentage points.
- Relative risk ratio, OMOP / PCORnet: `1.0720`.
- Absolute margin: not met.
- Relative margin: not met.
- Joint equivalence: not met.

### 30 days

| Metric | PCORnet | OMOP |
| --- | ---: | ---: |
| Eligible | 7,277 | 4,374 |
| Events | 1,178 | 753 |
| Risk | 16.1880% | 17.2154% |

- Absolute risk difference: `+1.0274` percentage points.
- Relative risk ratio, OMOP / PCORnet: `1.0635`.
- Absolute margin: not met.
- Relative margin: not met.
- Joint equivalence: not met.

These end-to-end failures do not indicate post-index acute-care outcome transformation failure. The fixed-index comparisons show exact outcome reproduction. The difference arises because the source and OMOP D0 cohorts differ upstream.

## Exploratory recurrent ischemic stroke, days 31-365

The implemented exploratory endpoint used the locked stroke code set in qualifying acute-care encounters/visits but did not apply a `PDX=P` filter to recurrent diagnoses.

Among 2,531 fixed-index patients with 365-day observability in both representations:

- PCORnet recurrent events: `263`.
- OMOP recurrent events: `258`.
- Label agreement: `2,526 / 2,531` = `99.80%`.
- Source-only positives: `5`.
- OMOP-only positives: `0`.

A post-outcome aggregate-only mechanism diagnostic reproduced these results exactly.

All five source-only recurrent-stroke patients had:

- an encounter-to-visit crosswalk;
- a corresponding OMOP visit;
- an OMOP acute-care visit concept;
- an OMOP visit within the locked day-31 through day-365 window;
- no diagnosis-to-condition crosswalk for the qualifying recurrent stroke diagnosis;
- therefore no linked OMOP condition occurrence.

None of the five discordances occurred at the day-31 or day-365 boundary. Their qualifying source events occurred between days 60 and 345. Index discharge dates agreed exactly for all 2,531 eligible patients.

Across all 2,144 source recurrent-stroke candidate diagnosis rows, all 2,144 retained visit lineage and valid acute-care timing, while 1,978 had diagnosis-to-condition lineage and 166 did not. The five patient-level recurrent-stroke discordances therefore arise from diagnosis materialization/condition-lineage loss, not encounter transformation or temporal-window drift.

### Post-outcome `PDX=P` sensitivity

Because the phrase "locked ischemic-stroke diagnosis semantics" could be read as carrying the source D0 `PDX=P` criterion into the recurrent endpoint, the completion supplement evaluated that interpretation as a post-outcome sensitivity rather than retroactively redefining the original exploratory endpoint.

Among the same 2,531 eligible patients:

| Metric | PCORnet | OMOP |
| --- | ---: | ---: |
| Recurrent events with `PDX=P` | 170 | 170 |
| Label agreement | 2,531 | 2,531 |
| Source-only positives | 0 | 0 |
| OMOP-only positives | 0 | 0 |

Thus, the `PDX=P` sensitivity was perfectly concordant. It is post-outcome and explanatory/sensitivity-only; it must not be represented as the originally implemented recurrent endpoint.

## Scientific interpretation

Stage D separates two distinct questions that would be conflated by a single end-to-end comparison.

First, post-index acute-care outcome representation is preserved exactly when patient and index date are held fixed. Both 30-day and 90-day event labels and risks are identical, 90-day first-event dates are exactly concordant, and the prespecified median time to first 90-day event is 26.0 days in both representations.

Second, end-to-end analytical equivalence fails because the source and lineage-faithful OMOP index cohorts differ substantially before outcome ascertainment. The Stage C diagnosis-date eligibility mechanism therefore propagates into downstream risks even though the outcome transformation itself is exact.

The originally implemented recurrent-stroke endpoint shows a small diagnosis-lineage loss (5 source-only positives), while the post-outcome `PDX=P` sensitivity is exactly concordant at 170 events in each representation. Together, these findings reinforce that recurrent-event interpretation depends on precisely specified diagnosis semantics and lineage requirements.

The appropriate manuscript conclusion is not that PCORnet and OMOP are globally equivalent or nonequivalent. The evidence supports exact downstream outcome fidelity conditional on a shared index cohort, but not end-to-end analytical equivalence when cohort construction depends on source semantics and ETL eligibility rules that are not preserved for all patients.

## Disclosure

All committed Stage D manuscript-oriented outputs and mechanism diagnostics are aggregate only. No patient identifiers, source-record identifiers, row-level protected health information, or free-text clinical values are committed.

## Closure

Stage D is closed for routine analysis after completion of the omitted prespecified median time-to-event metric and documentation of the post-outcome recurrent `PDX=P` sensitivity. Do not modify the frozen ETL, D0 definition, Stage D primary/secondary outcomes, observability rules, or equivalence margins based on these results. Additional Stage D analyses should be limited to reviewer-driven sensitivity analyses or independently demonstrated defects.
