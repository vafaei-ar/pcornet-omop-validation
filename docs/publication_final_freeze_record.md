# Final publication analysis freeze record

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Publication analysis branch: `publication/analysis`

This record marks Stages A-D as analytically closed for manuscript production. It freezes the quantitative claims and interpretation hierarchy that may appear in the main manuscript. New exploratory analyses should not be added unless required by peer review or an independently demonstrated defect.

## Closed analytical stages

- **Stage A — structural transformation:** complete.
- **Stage B — mapped patient-level semantic concordance:** complete and locked.
- **Stage C — ischemic-stroke phenotype reproducibility:** complete and locked.
- **Stage D — downstream analytical equivalence:** complete after protocol-completion supplement and post-outcome recurrent-stroke sensitivity.

## Provenance anchors

- Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`
- Stage C D1/D3 completed concordance SHA: `96131db8e47ba89a909438d6208502c6c7cdbea3`
- Stage D outcome-free preflight SHA: `6e6d2139ab09648b5c59770788ce7762ed5ffb7d`
- Stage D completed analysis SHA: `1e4508ff55928965249beb843646232f3a234048`
- Stage D recurrent discordance diagnostic SHA: `6321336996cd826dfade84509a81691a8aaaf260`
- Stage D completion-supplement SHA: `c97a3e65880b58fb39cf66c548164d46c61fbc5d`
- Stage D study-definition SHA-256: `e8a0914bee9993f34167ad2b336191f2a0c52e88e47adb99c9f2c94cd6f7abbf`
- Inherited D0-definition SHA-256: `45c4fe08493e6a1713b2d1b38b6a6903e67784a1edcb5d01de119cccc3489418`

## Quantitative claims frozen for the manuscript

### Stage A

- DIAGNOSIS: `11,484,577` source rows; `3,459,785` excluded for missing `DX_DATE`.
- PROCEDURES: `11,244,947` source rows; `16,924` excluded for missing `PX_DATE`.
- Drug route ledger: `17,469,480` concept-zero routes.
- Final target counts remain those documented in the Stage A completion record.

### Stage B

Every mapped source semantic event or route in the prespecified denominators was found in native OMOP. Target-side excess for Condition and Procedure was explained by other audited source provenance. Resolved UCUM units and prespecified categorical value concepts agreed exactly. VITAL numeric differences were fully explained by the frozen ETL SQL expression, leaving zero unexplained target mismatches.

### Stage C

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | Source only | OMOP only | Patient Jaccard | Exact shared index date |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 9,815 | 6,001 | 6,001 | 3,814 | 0 | 0.611411 | 100.00% |
| D1 | 8,624 | 5,246 | 5,245 | 3,379 | 1 | 0.608116 | 97.39% |
| D3 | 7,565 | 4,710 | 4,709 | 2,856 | 1 | 0.622390 | 97.52% |

The dominant D1/D3 source-only and shared-index-date mechanisms are frozen as post-outcome explanatory findings: null selected `DX_DATE` and loss of diagnosis lineage under the required-date ETL rule.

### Stage D

| Estimand | PCORnet eligible | OMOP eligible | PCORnet events | OMOP events | PCORnet risk | OMOP risk | Absolute difference, pp | Risk ratio | Equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Fixed-index 90 d | 3,822 | 3,822 | 1,132 | 1,132 | 29.6180% | 29.6180% | 0.0000 | 1.0000 | Met |
| Fixed-index 30 d | 4,374 | 4,374 | 753 | 753 | 17.2154% | 17.2154% | 0.0000 | 1.0000 | Met |
| End-to-end 90 d | 6,508 | 3,822 | 1,798 | 1,132 | 27.6275% | 29.6180% | +1.9905 | 1.0720 | Not met |
| End-to-end 30 d | 7,277 | 4,374 | 1,178 | 753 | 16.1880% | 17.2154% | +1.0274 | 1.0635 | Not met |

Additional frozen Stage D findings:

- First-event date agreement among 90-day both-positive patients: `1,132 / 1,132` exact.
- Median days to first 90-day event: `26.0` in PCORnet and `26.0` in OMOP.
- Implemented exploratory recurrent endpoint: `2,531` eligible, `263` source events, `258` OMOP events, `2,526` agreements, `5` source-only positives, `0` OMOP-only positives.
- Post-outcome `PDX=P` recurrent sensitivity: `170` source events, `170` OMOP events, `2,531 / 2,531` label agreement.

## Interpretation hierarchy frozen for manuscript use

1. Structural row-count differences are not sufficient evidence of transformation failure.
2. Mapped semantic preservation and vocabulary coverage are separate questions.
3. Complete phenotype reproducibility can be materially lower than mapped-event concordance when source-model semantics interact with ETL eligibility rules.
4. Fixed-index downstream outcome fidelity can be exact even when end-to-end analytical equivalence fails because cohort construction changed upstream.
5. The study does **not** establish global PCORnet/OMOP equivalence or intrinsic OMOP bias.

## Main manuscript tables

The default main-text table set is frozen as:

- **Table 1:** Stage A structural representation, eligibility, exclusions, and mapping coverage.
- **Table 2:** Stage B mapped semantic concordance and resolved-value agreement.
- **Table 3:** Stage C stroke phenotype reproducibility and mechanism summary.
- **Table 4:** Stage D fixed-index versus end-to-end analytical equivalence.

Detailed domain-level mapping mechanisms, native-OMOP portability sensitivities, and recurrent-stroke diagnostics should be placed in supplements unless required by the target journal.

## Main manuscript figures

The default figure set is frozen conceptually as:

- **Figure 1:** Layered validation framework from source transformation through analytical equivalence.
- **Figure 2:** Mechanism of phenotype attrition caused by the required diagnosis-date rule.
- **Figure 3:** Stage D contrast between exact fixed-index outcome fidelity and failed end-to-end equivalence.

Figure styling, dimensions, labels, and journal-specific formatting may change without altering the frozen scientific message.

## Disclosure and reproducibility freeze

Main-text and supplement artifacts intended for publication must remain aggregate only. Do not commit patient identifiers, source-record identifiers, row-level PHI, free-text clinical values, or row-level disagreement extracts.

Before external submission, verify that:

- all manuscript numbers trace to a locked completion record or aggregate result artifact;
- all post-outcome audits are labeled explanatory or sensitivity analyses;
- no denominator is silently changed during copyediting;
- no equivalence margin is altered;
- no new exploratory result is added to the main narrative without a documented reason;
- study-definition and ETL provenance are retained in reproducibility materials.

## Closure rule

Stages A-D are closed for routine analysis. The next phase is manuscript production, journal targeting, figure rendering, reference completion, and submission-specific formatting. Reviewer-driven sensitivity analyses should be versioned separately and must not overwrite the frozen primary results.