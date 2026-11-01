# 09 - Post-freeze QC evidence record and manuscript finalization gates

_Last checked against the GitHub post-freeze branch: 2026-10-31._

**Scope:** This document records the post-freeze QC work and converts it into
publication decisions. It does **not** redefine the study, attest to a
completed DQD run, or replace the canonical A-E results in
[`03_RESULTS_THROUGH_STAGE_E.md`](03_RESULTS_THROUGH_STAGE_E.md).
For commands, full safety evidence and chronological troubleshooting use
[`08_POSTFREEZE_STANDARD_QC_AND_INTERPRETABILITY.md`](08_POSTFREEZE_STANDARD_QC_AND_INTERPRETABILITY.md).

## 1. Frozen study and publication source of truth

- Frozen publication ETL: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.
- Publication baseline on `main`: `1c271c37a9d1a279f075a8edee439fe0cce7706f`.
- The post-freeze QC extension is developed on
  `postfreeze/dqd-achilles-interpretability-20261029`, **separately from
  `main`**. It is not an amendment to the locked A-E results.
- Stage A-E scientific definitions and their main quantitative findings are
  documented in `02_STUDY_DESIGN_AND_DECISIONS.md`,
  `03_RESULTS_THROUGH_STAGE_E.md`, and locked JSON definitions.
- Canonical figure/table generator inputs and commands are in
  `07_PUBLICATION_FIGURES.md`; the current aggregate asset is
  `study_definitions/artifacts/publication_figure_data_v2.json`.
  Original `v1` stays frozen.
- Individual patient records, small-cell counts, local SQL configuration,
  passwords and private prediction/overlap files must not be committed.

## 2. Verified post-freeze QC record

| Question | Recorded evidence / observed status | Defensible conclusion |
| --- | --- | --- |
| Can QC read the frozen target without writing clinical tables? | Dedicated restricted `ohdsi_postfreeze_qc` login, separate QC output database, JDBC/ODBC access and the comprehensive 75-table permissions audit passed on 2026-10-30. | The standard QC work was separated from ETL writes. This is not proof every possible query is safe or that the CDM is complete. |
| Was a recoverable backup established? | Local 23.03 GiB compressed backup, SQL Server backup history and `RESTORE VERIFYONLY WITH CHECKSUM` passed; the frozen reference row-count anchors stayed unchanged. | Local backup verification passed. **Off-machine protected copy and isolated test restore are still pending.** |
| Did DQD finish? | No. The frozen `CDM_SOURCE` table contains no rows; direct inspection confirmed the metadata deficiency. The DQD runner was blocked before delivering check results. | **No DQD pass rate, DQD failures or global quality certification may be reported.** |
| Did Achilles run? | Achilles 1.7.2 completed; 194 default analysis IDs executed and their performance records were retained. The persisted-results audit found 294 definition rows, 840,097 regular result rows and 44,802 distribution rows. No permanent scratch tables remained. | Completed descriptive target characterization, not independent validation of PCORnet-to-OMOP correspondence. |
| Why were 54 default analysis outputs absent from the data-bearing results? | 50 corresponded to categories whose OMOP source domains were empty; 4 were IDs 207, 209, 210 and 814 on populated domains. Source-table presence was checked read-only. | Distinguish **empty input domains** from **suppressed count outputs**. Neither a missing Achilles row nor a successful run certifies quality. |
| What do the four remaining missing IDs test? | Official Achilles 1.7.2 SQL: 207 invalid visit person reference, 209 invalid nonnull visit care-site reference, 210 visit start outside any observation period, 814 observation with all three value fields null. `smallCellCount=10` suppresses counts at or below that threshold. | Their missing data-bearing result rows are consistent with small-cell suppression. Never publish protected exact values without review. |
| Was analysis 210 independently checked? | The four-ID private diagnostic ran and its file passed structural/permission checks. The overlap tool took the prior-result zero-exception shortcut and did **not** re-materialize a D0 cohort. On 2026-10-31 the restricted live SQL count for 210 matched that private prior result; frozen row-count anchors passed. | **Analysis 210 live count reconciliation is complete.** Do not claim an independently rerun D0 analysis or a recalculated outcome estimate. No further 210 script is warranted on the current unchanged snapshot. |
| Was clinical row coverage complete? | Eight target tables are empty: `visit_detail`, `payer_plan_period`, `condition_era`, `drug_era`, `care_site`, `location`, `provider` and `note`. `CDM_SOURCE` is also empty. The original Phase 12 acceptance gate required only 11 populated target tables. | **Limited OMOP domain completeness** despite successful original study-specific acceptance checks. The design proposed `care_site`, `location` and `cdm_source` but the frozen implementation did not deliver them. Provider source input was absent. |
| Were changes tested? | Institutional `pytest -q` on 2026-10-31: **193 passed, 1 skipped**, and live analysis-210 reconciliation reported PASS. | This verifies the reported code-check baseline and the stated local diagnostic, not global CDM validity. |
| Were the planned post-freeze RF/XGBoost attribution analyses run? | The institutional terminal reports successful 16-model computation, including RF/XGBoost for four populations and both six/five-feature sets, with SHAP and 20-repeat held-out permutation importance. The private aggregate remains on the institutional machine; see document 08. | **Executed; private numerical audit PASSED (234 tests) and a protected scientific review was created (238 tests). Training-perturbation sensitivity and institutional disclosure remain open.** No numerical or directional attribution claim should enter the manuscript yet. |

The protected exact-count and analysis-210 evidence lives locally under
`results/postfreeze_standard_qc/private_achilles_exception_counts/`.
These artifacts are ignored by Git and require institutional disclosure
review. Commit only sanitized statements and allowed aggregates.

### Scope distinction essential for peer review

The completed **A-E study** tested a source-aware, study-specific
transformation and scientific reproducibility. The unfinished **DQD**
extension and the empty target domains do not undo the observed original
fidelity results; they do limit any claim that the resulting database is a
fully populated, DQD-certified OMOP implementation.

The primary result remains the one in document 03: the frozen diagnosis-date
eligibility policy made source-faithful stroke cohorts diverge; fixed-index
outcome comparisons were exact; and independently selected cohorts yielded
different risks and model performance. Harmonized and provenance-aware
date-fallback sensitivities are **post-freeze mechanism analyses**. They
do not replace the original study's primary estimand.

### RF/XGBoost feature-importance comparison: planned scientific extension

**Study-team objective reaffirmed on 2026-10-31:** Assess whether the
end-to-end PCORnet-versus-frozen-OMOP data/modeling pathways change
**predictor importance** for 90-day acute-care utilization, using both
**Random Forest and XGBoost**. This is not interchangeable with the
original Stage E histogram-gradient-boosting prediction comparison.

The protocol `study_definitions/postfreeze_standard_qc_interpretability_v1.json`
already locks the same six Stage E features, outcome and 70/30 patient
hash split. The end-to-end contrast is `pcornet_full` (6,508 eligible,
1,798 events) versus `omop_frozen` (3,822 eligible, 1,132 events).
That contrast combines **population selection** and **representation**,
so it must not be interpreted alone as a data-model effect.

The planned four-population design separates mechanisms:

| Paired comparison | Interpretation |
| --- | --- |
| `pcornet_full` vs `omop_frozen` | End-to-end difference: selection and representation combined |
| `pcornet_full` vs `pcornet_shared` | Population selection within PCORnet |
| `pcornet_shared` vs `omop_frozen` | Representation with patients, index dates and outcomes aligned |
| `pcornet_full` vs `omop_fallback` | Provenance-aware target fallback with full source population matched |

The **primary importance measure** is repeated held-out AUROC permutation
importance. Positive-class probability-scale interventional SHAP mean
absolute values and rank changes are secondary. Compare native test sets
and the same matched shared test patients. Include the prespecified
five-feature sensitivity removing the prior-stroke history flag, since it
requires a recorded diagnosis date. Correlated utilization predictors may
exchange importance. Differences in rankings are **not causal effects**.

**Execution gate (currently open):** Review the private exporter and
interpretability engine against the locked Stage C/D/E definitions before
any institutional extraction. Verify input lineage, identities, overlap,
index dates, outcomes, features, split rules and reference SHAP sampling.
The SHAP background previously selected the first sorted training IDs,
which conflicted with the locked fixed-seed sampling protocol. The model
engine and synthetic tests now implement the fixed-seed sample. The exporter
also gained protected-path and input-integrity checks plus a no-SQL preflight.
The model engine now sorts patient IDs before fitting/sampling so fixed seeds
are robust to arbitrary SQL row order, without changing the 70/30 split.
**Local execution update (2026-10-31):** 201 tests passed after the
interpretability dependencies were installed. The new `--preflight-only`
run blocked on the missing saved Stage E preflight JSON before any SQL or
patient-data extraction. The Stage E preflight originally passed in
September 2026. A subsequent local file inventory confirmed no copy
of `stage_e_statistical_model_preflight.json` exists under the ignored
`results/` tree, while the Stage C fallback result is present.
`scripts/recover_postfreeze_stage_e_preflight.py` now provides an
outcome-free, create-only recovery using the restricted QC account and
clearly labels any regenerated metadata as a new execution. **The
recovery code and new tests have not run on the institutional host.**
Original frozen definitions, ETL data and publication conclusions remain
locked. **2026-11-01 update:** Institutional tests passed again (**209 passed**).
The authentic Stage C fallback record was already present. The missing
Stage E outcome-free preflight was regenerated and explicitly labeled,
using only the restricted QC login, and a new no-SQL feature-export
preflight passed. No row-level export or model fitting was performed.
The protected exporter has now been further hardened to use the same
restricted QC login for **both** locked Stage E and fallback Stage D
queries, restore engine bindings, and suppress provider secrets.
The first updated institutional pytest reported **210 passed, 3 failed**:
all failures came from incorrectly importing the top-level `scripts`
directory as a Python package. Pytest stopped the chained command before
any patient-level extraction. The exporter had the same import defect.
The restricted frozen-target checks now reside in importable package code;
the exporter and regression tests were corrected, and additional synthetic
frozen-anchor/identity tests were added. **Subsequent institutional execution (2026-11-01):** The import fix passed
the full synthetic test suite (**221 tests passed**). The restricted
exporter ran Stage E and the Stage D fallback with exact locked anchors,
and four protected Parquet files were saved under ignored
`results/private_inputs/` for 6,508/3,822/3,822/6,508 patients,
respectively. This is a **completed extraction**, not a completed
attribution analysis. The manuscript's original Stage A-E results remain
unchanged.

The model code was then revised to use the **full held-out sets** for
primary AUROC permutation importance, keeping a bounded common
training-reference SHAP sample; to use the matched shared test set
for selection/representation contrasts; to compute explicit pairwise
rank and magnitude differences; and to protect both private input
files and create-only aggregate output. **Subsequent institutional validation (2026-11-01):** The
revised modeling code passed all **227 tests** and the full locked
four-population `--validate-only` gate reported
`RF XGBOOST PRIVATE INPUT VALIDATION: PASS`. Patient alignment,
index dates, 90-day outcomes and split membership were verified without
fitting models or writing files.

**Subsequent institutional computation (2026-11-01):** All 16
RF/XGBoost model fits, held-out permutation analyses and bounded SHAP
analyses completed; the terminal reported
`RF XGBOOST PRIVATE AGGREGATE ANALYSIS: COMPLETE`. The private file was
not uploaded or published. The main question remains whether predictor
importance changes are quantitatively meaningful and robust, which
cannot be decided from completion logs alone.

A protected, read-only aggregate audit and synthetic tests were then
added. The audit will verify provenance, all four comparison scopes,
the 16-model matrix, full-test denominators, feature ranks, and the
arithmetic of right-minus-left importance changes without printing
scores or exporting protected data. **Subsequent institutional audit (2026-11-01):** The full test suite
returned **234 passed** and the protected audit reported
`RF XGBOOST PRIVATE AGGREGATE AUDIT: PASS`. The completed model matrix,
denominators, AUROC permutation importance, SHAP/rank structure,
contrast arithmetic and provenance passed the automated checks. No
private output was uploaded or disclosed.

**Local backup inventory update (2026-11-01):** The operator
confirmed one existing `OMOP_VALIDATED_A_copyonly_*.bak` backup
candidate totaling **23.03 GiB**. The `mssql` service account
passed a read-only backup **directory** access check. Previously
recorded SQL Server backup history and `RESTORE VERIFYONLY WITH
CHECKSUM` remain the strongest local media verification evidence.
Individual file readability, encrypted off-machine retention,
copy-integrity checks and isolated full restore are still open.
The existing backup must not be unnecessarily regenerated, deleted,
uploaded to chat/Git, or copied to an unapproved destination.
A separate isolated restore is a prerequisite to any optional DQD
metadata amendment and evaluation. The frozen publication database
must remain unchanged.

**Institutional robustness execution (2026-11-01):** The corrected
synthetic suite returned **245 passed in 2.32s**, followed by
`RF XGBOOST PRIVATE STABILITY REVIEW: COMPLETE`. The restricted
training-perturbation and predictor-diagnostic report was created
locally. No protected scores, patient-level records or feature rankings
were exported. The four-population, six-/five-feature RF/XGBoost
analysis has now completed the computational, schema/arithmetic,
interpretation-report-generation and descriptive robustness phases.
It has **not** completed scientific effect-size interpretation,
patient-level uncertainty or institutional disclosure clearance.
Do not present attribution changes as significant or causal from the
five perturbations or 20 shuffles.

The next independent infrastructure gate is the existing 23 GiB
COPY_ONLY backup's approved encrypted off-machine copy and a
verified isolated restore. The frozen target must remain unchanged;
the isolated restored copy is also the correct place to evaluate
DQD after factual CDM_SOURCE provenance metadata are established.
Protected backup transfer requires an operator-authorized target
rather than a guessed destination.

**Robustness gate update (2026-11-01):** The first local suite after
adding stability checks yielded **243 passed, one failed**. The
single failing test supplied a placeholder synthetic aggregate in
place of the full attribution structure required by the reviewer.
The chain stopped before the 80-refit analysis. The fixture was
replaced with the existing complete synthetic aggregate, and an
independent small RF/XGBoost permutation-fitting regression test
was added. **Neither the corrected tests nor private stability
analysis have yet run on the institutional host.** The original
protected attribution and scientific-review outputs remain intact.
Scientific robustness and disclosure clearance remain open.

**Subsequent institutional review generation (2026-11-01):**
The local reviewer passed **238 tests** and reported
`RF XGBOOST PRIVATE SCIENTIFIC REVIEW: WRITTEN`. The report records
RF/XGBoost feature/rank/magnitude shifts for native end-to-end,
matched selection, matched frozen representation and full fallback
representation, for six- and five-feature models. It remains an
owner-only, ignored Markdown report on the institutional machine;
no results, numerical tables or clinical identifiers were uploaded.
Completion of report creation is not a scientific interpretation
or disclosure approval.

A new optional-but-study-team-prioritized `postfreeze_interpretability_stability`
module now computes training-only predictor missingness and Spearman
correlations, matched source-target predictor differences, and
permutation importance/rank-direction sensitivity across 80 fits
(one full training baseline and four patient-ID-aligned 90%-training
perturbations for each of the 16 analysis cells). It writes a separate
create-only, private 0600 Markdown review. Its new synthetic tests
and the institutional robustness computation **have not yet run**.
This training perturbation does not provide patient-bootstrap confidence
intervals or statistical-significance evidence, nor does it validate
SHAP stability. If the study claims inferential evidence for changed
importance, design patient-resampling inference explicitly and check
correlated features. Obtain institutional disclosure approval before
any numeric figure or manuscript interpretation.

This study-team-prioritized analysis should be completed **before making
RF/XGBoost attribution claims or closing a manuscript version that
intends to include them**. A no-attribution version of the A-E paper
remains scientifically possible, but omission is an explicit editorial
choice, not a claim that the planned analysis was done.

## 3. Manuscript source and version audit

**Unresolved handoff issue:** No current manuscript DOCX is tracked in this
repository. Document 08 refers to a **latest v1.6.2** manuscript and to
Qiran's comments on **v0.2**. Document 07's figure-placement example still
targets **v1.3 JAMIA reader-focused**. Those references cannot all serve
as the same final Word source. The old integrated Markdown fragments under
`docs/archive/` are historical, not parallel active drafts.

Before Word edits, **locate the actual latest author-approved v1.6.2 file**,
confirm whether the target is JAMIA, and establish a single current source
with a checksum/version and source of reviewer comments in a *safe*
manuscript handoff record. Do not silently replace newer text with v1.3
or regenerate the manuscript from an archived fragment. When revising Word,
preserve real tracked changes, comments, figure references and Zotero fields.

## 4. Priority gates to manuscript finalization

| Gate | Required action and evidence | Category | Current status |
| --- | --- | --- | --- |
| 1. Evidence closure | Keep this QC closeout, document 08, versioned source code and institutional PASS transcript aligned; verify protected files remain private. Preserve frozen anchors. | Essential provenance | **Recorded on this branch** |
| 2. Resolve manuscript version | Obtain the actual latest DOCX (not the v1.3 example), prior comments and a journal target. Confirm which changes already landed. | **Hard writing blocker** | Open |
| 3. Choose QC publication scope | Decide explicitly whether the paper reports only the completed original A-E study plus clearly labeled Achilles limitations, or also claims new DQD findings. | Author scientific/editorial decision | Open |
| 4A. Minimal publication route | If no new DQD/SHAP claim is needed, describe empty `CDM_SOURCE`, the unpopulated domains, limited 11-table freeze gate, unavailable source PROVIDER, and Achilles's descriptive/suppressed coverage. State DQD was **not completed**. | Sufficient to proceed with original A-E paper if journal/reviewers accept this scope | Available |
| 4B. Optional expanded QC route | If DQD results are required, first make a protected off-machine backup and isolated **restored** analysis copy; verify source provenance and populate `CDM_SOURCE` **only** with factual, documented metadata in that copy. Independently verify clinical table count/hash anchors; run version-pinned DQD on that copy, review all default check results and failures, pass disclosure review, label the amended target clearly. Do not edit the frozen publication target. | Conditional experimental extension, **not** automatically a manuscript gate | Not started |
| 5. Resolve manuscript claims for 207/209/814 | Use original SQL predicates, known null-link policy and observation semantics; review private classifications only locally. The paper may report their presence as suppressed/unresolved, but must not claim all three passed. | Essential **only** if paper claims those quality properties | Open |
| 6. Integrate accepted evidence | Align Abstract, Methods, Results, Discussion and limitations with locked definitions, primary versus post-freeze results, and source/target conditioning. Keep mechanistic and attribution claims proportional to evidence. | Hard scientific/narrative gate | Not reviewed against latest DOCX |
| 7. Verify paper numbers and visuals | Rebuild Figures 1-4, Extended Data Figures 1-3, Main Tables 1-3 and S1-S18 from the v2 aggregate artifact; verify scientific invariants, inspect exported figure legibility, compare every manuscript number/denominator/CI to the exact locked aggregate and captions, avoid introducing a second plotting pipeline. | Hard final QA gate | Code/pipeline documented; latest Word synchronization not verified |
| 8. Manuscript production | Revise the *one* latest DOCX with granular tracked edits and intact Zotero citations; incorporate relevant Qiran suggestions without reverting newer science; update journal-specific checklist, references, supplement numbering, figure placement and cover letter. | Hard submission gate | Open |
| 9. Independent signoff | Coauthor scientific review, disclosure and governance review, journal reporting compliance, Word/PDF render inspection, final artifact manifest and Git tag/PR release review. | Hard submission gate | Open |

### What should not hold the manuscript hostage

- A new Achilles rerun or more analysis-210 diagnostics on the unchanged frozen data.
- RF/XGBoost model attribution has now **executed locally** for all
  16 fits. Its output is awaiting a private numerical/provenance audit,
  scientific interpretation and institutional disclosure approval.
  It does not require DQD or reopening the frozen ETL.
- A complete native OMOP domain backfill into the **frozen** publication
  database. That would change the object under study and undermine the freeze.

### What **does** block unsupported claims

- Calling DQD completed or passed while `CDM_SOURCE` is empty and no DQD
  result exists.
- Calling this a fully populated OMOP CDM despite the empty domains.
- Claiming missing Achilles analysis rows prove no data-quality exceptions.
- Attributing end-to-end differences to the OMOP standard intrinsically,
  rather than the tested ETL policy and specific study population.
- Using a manuscript version that predates reviewed figure/mechanism changes
  without reconciling intervening Word edits.

## 5. Proposed editorial sequence (after identifying the current DOCX)

1. **Lock message and displays:** preserve source-faithful D0/D1/D3 and
   30/90-day end-to-end comparisons as primary. Use fixed-cohort results to
   localize the mechanism. Show the recorded-date restriction and encounter
   fallback as explicitly labeled sensitivities; do not redefine the
   original estimand.
2. **Methods audit:** disclose dataset/site, OMOP version and frozen scope,
   missing-date rule, lineage/PDX, follow-up eligibility, model features,
   estimands, threshold rationale, incomplete mapped/unmapped coverage,
   sensitivity timing, and QC run statuses.
3. **Results/figures:** make Figure 2/3 the central phenotype/outcome mechanism
   evidence. Keep Stage B semantic fidelity and Stage E model propagation
   appropriately subordinate. Avoid describing effect magnitudes as
   generalizable across ETLs or sites.
4. **Discussion:** distinguish ETL defect, explicit policy, CDM representation,
   vocabulary gaps and unsupported domains; present the QC limitation
   without falsely portraying DQD as a failed clinical quality test.
5. **Final source audit:** cross-check abstract, all tables/captions,
   supplementary files and references, then render and inspect the latest
   tracked DOCX and submission PDF. Require scientific and disclosure signoff
   before submission.

**Finalization verdict as of 2026-10-31:** Original A-E results and the
analysis-210 verification are documented. **The manuscript is not yet ready
for submission** because the authoritative Word version, new QC scope
decision, current Word-to-figure/table synchronization and final
coauthor/disclosure checks have not been verified. DQD remains an optional, uncompleted QC extension;
RF/XGBoost attribution computation and the private structural/numeric
audit have completed, but scientific robustness and disclosure clearance
remain outstanding. DQD is not completed. Neither should be presented
as a validated published finding without the remaining checks. This is an evidence and
editorial readiness distinction, not a request to reopen the locked ETL.
