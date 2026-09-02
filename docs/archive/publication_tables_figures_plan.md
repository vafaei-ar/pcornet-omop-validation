# Publication tables and figures plan

_Last updated: 2026-09-02_

This document defines the default main-text display strategy after analytical closure of Stages A-D. It freezes scientific content, not journal-specific typography.

## Table 1 — Structural transformation and explicit coverage

Purpose: show why raw source-to-target row counts are not a valid standalone fidelity criterion.

Recommended rows:

| Layer | Source / denominator | Represented / mapped | Excluded / unresolved | Key interpretation |
| --- | ---: | ---: | ---: | --- |
| DIAGNOSIS | 11,484,577 | 8,024,792 eligible | 3,459,785 excluded | Missing `DX_DATE` required by frozen ETL. |
| PROCEDURES | 11,244,947 | 11,228,023 eligible | 16,924 excluded | Missing `PX_DATE`. |
| OBS_CLIN | 38,850,928 | 38,850,928 routed | 12,737 concept-zero | Cross-domain routing with low unresolved fraction. |
| Drug route ledger | 48,457,880 | 30,988,400 mapped Standard Drug routes | 17,469,480 concept-zero | Vocabulary/source-code coverage, not mapped-event loss. |
| Condition canonical routing | 8,674,973 source events | 9,045,157 route rows | 60,148 concept-zero fallback | One-to-many/cross-domain routes prevent one-to-one row interpretation. |

Keep full final OMOP table inventory in supplement unless the target journal has generous table space.

## Table 2 — Patient-level semantic preservation

Purpose: distinguish exact mapped agreement from unresolved semantic coverage.

Recommended rows:

| Semantic family | Source mapped rows | Exact matched | Source unmatched | Target rows in semantic space | Other provenance | Unresolved / concept zero |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Encounter | 1,510,957 | 1,510,957 | 0 | 1,510,957 | 0 | 0 |
| Death | 6,955 | 6,955 | 0 | 6,955 | 0 | 0 |
| Condition | 8,983,621 | 8,983,621 | 0 | 9,739,734 | 756,113 | 60,148 |
| Procedure | 11,121,561 | 11,121,561 | 0 | 12,659,204 | 1,537,643 | 111,660 |
| Drug | 30,988,400 | 30,988,400 | 0 | 30,988,448 | 48 | 17,469,480 |
| Measurement/Observation | 92,668,145 | 92,668,145 | 0 | 92,668,145 | 0 | 366,371 |

Footnote or companion panel:

- Directly comparable numeric rows: 75,769,622; unexplained residual mismatches after frozen-expression audit: 0.
- Resolved UCUM units: 58,916,347 / 58,916,347 exact.
- Prespecified mapped categorical value concepts: 809,630 / 809,630 exact.

## Table 3 — Stroke phenotype reproducibility

Purpose: show the contrast between mapped-event preservation and complete computable-phenotype reproducibility.

| Phenotype | PCORnet | Lineage-faithful OMOP | Shared | PCORnet only | OMOP only | Jaccard | Exact shared index date |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D0 | 9,815 | 6,001 | 6,001 | 3,814 | 0 | 0.611 | 100.00% |
| D1 | 8,624 | 5,246 | 5,245 | 3,379 | 1 | 0.608 | 97.39% |
| D3 | 7,565 | 4,710 | 4,709 | 2,856 | 1 | 0.622 | 97.52% |

Mechanism note: all D1/D3 source-only patients had null selected `DX_DATE` and lacked diagnosis lineage under the frozen required-date rule; all shared-index-date mismatches were explained by selection of another qualifying episode after loss of the source-selected diagnosis.

Native-OMOP portability Jaccard values (`0.565`, `0.583`, `0.601`) should be supplemental unless space permits.

## Table 4 — Analytical equivalence

Purpose: make the conditional-versus-end-to-end distinction immediately visible.

| Estimand | PCORnet eligible | OMOP eligible | PCORnet events | OMOP events | PCORnet risk | OMOP risk | Absolute difference, pp | Risk ratio | Equivalence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Fixed-index 90 d | 3,822 | 3,822 | 1,132 | 1,132 | 29.618% | 29.618% | 0.000 | 1.000 | Met |
| Fixed-index 30 d | 4,374 | 4,374 | 753 | 753 | 17.215% | 17.215% | 0.000 | 1.000 | Met |
| End-to-end 90 d | 6,508 | 3,822 | 1,798 | 1,132 | 27.628% | 29.618% | +1.990 | 1.072 | Not met |
| End-to-end 30 d | 7,277 | 4,374 | 1,178 | 753 | 16.188% | 17.215% | +1.027 | 1.063 | Not met |

Footnotes:

- Prespecified margins: absolute risk difference within ±0.5 percentage points and OMOP/source RR 0.95-1.05.
- Fixed-index 90-day median days to first event: 26.0 in both representations; 1,132 / 1,132 first-event dates matched exactly.

The recurrent-stroke endpoint belongs in supplement because it is exploratory and includes a post-outcome definition sensitivity.

## Figure 1 — Layered validation framework

Scientific message: validation answers four different questions, each conditional on the previous layer.

Recommended flow:

`PCORnet source → audited ETL rules/routing → OMOP representation → mapped semantic concordance → complete phenotype reproducibility → downstream analytical equivalence`

Side annotations should distinguish:

- exclusions / concept zero / cross-domain routing;
- mapped semantic preservation;
- source-model semantics such as `PDX`;
- fixed-index versus end-to-end estimands.

## Figure 2 — Where stroke phenotype attrition enters

Scientific message: the dominant phenotype loss occurs before imaging/lipid evidence is considered.

Recommended flow:

`Stroke diagnosis with null DX_DATE → source phenotype can use encounter-date fallback → frozen ETL excludes diagnosis → no diagnosis lineage in OMOP → patient lost or later qualifying episode selected`

Include two terminal effects:

- source-only phenotype patient;
- different selected index episode among shared patients.

## Figure 3 — Conditional versus end-to-end analytical equivalence

Scientific message: outcome transformation can be exact while end-to-end estimates differ because the cohort changed upstream.

Recommended visual:

Two paired pathways:

1. **Fixed-index:** shared patients + exact index dates → identical 30/90-day labels and risks → equivalence met.
2. **End-to-end:** independently constructed PCORnet and OMOP D0 cohorts → different eligible populations → risk difference 1.03-1.99 pp → equivalence not met.

Do not present the end-to-end difference as an intrinsic OMOP effect.

## Supplemental displays

Recommended supplemental items:

- full Stage A routing/domain inventory;
- Stage B resolved unit/value coverage details;
- Stage C native-OMOP portability sensitivities;
- Stage C source-only and index-date mechanism audits;
- Stage D recurrent-stroke implemented endpoint and `PDX=P` post-outcome sensitivity;
- provenance/analysis SHA table and study-definition hashes.

## Display freeze rule

Numbers and scientific labels in these displays are frozen. Formatting, ordering of columns, abbreviation style, footnote wording, and figure aesthetics may be adapted to journal requirements without changing denominators, estimands, margins, or interpretation.