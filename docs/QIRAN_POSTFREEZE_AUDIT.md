# Qiran post-freeze analysis: selective loss, coding era, duplicates, person coverage

## Status

**Code prepared and offline-tested. Not executed on institutional clinical data. No new findings have been generated.** This audit is a read-only, aggregate-only extension of the publication study. It **does not change the frozen ETL, D0/D1/D3 phenotype, or Stage D/E results**. No row-level data leave SQL Server.

Repository reference: `vafaei-ar/pcornet-omop-validation`.
Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.
Source definitions: `study_definitions/stage_c_stroke_d0_v1.json`, `src/pcornet_omop_validation/study/stroke_codes.py`.
Locked Stage E feature logic: `src/pcornet_omop_validation/study/stage_e_statistical_model_reproducibility.py`.
Frozen ETL route logic: `src/pcornet_omop_validation/etl/condition_occurrence.py`, `condition_canonical_routes.py` at the frozen SHA.

This package was built outside the public repository to avoid sharing unreviewed code or results without author approval.

## Four analyses

1. **D0 source-only versus shared patients**: same locked source D0 index, two groups (source-only and shared). Report group size, sex, age, length of stay, 365-day prior all/acute encounter counts, source-only minus shared standardized mean differences, encounter type, and index date/year. This is a D0 comparison, **not** the 90-day outcome-eligible comparison.
2. **Date-missingness and code era**: (a) all primary ischemic-stroke DIAGNOSIS records linked to qualifying EI/IP overnight encounters, with **admission-year denominator** and DX_DATE missing count, stratified by code-defined ICD-9-CM versus ICD-10-CM; (b) final D0 index patient-group year/era distributions. The record-level candidate sample and patient-level D0 sample must not be combined into one denominator. Code lists, not DX_TYPE, determine the locked stroke code-system category.
3. **Cross-stream DIAGNOSIS + CONDITION exact overlap**: match eligible records by PATID, ENCOUNTERID, normalized source code, code system, and event date (DX_DATE vs COALESCE(ONSET_DATE, REPORT_DATE)); output counts of matching event keys/records only. These are *candidate overlaps*, not confirmed duplicate clinical assertions or OMOP defects. Missing-date DIAGNOSIS rows are not eligible for the frozen ETL and are not part of this match. Vocabulary one-to-many routes are also not duplicates by themselves.
4. **Person coverage**: raw PCORnet DEMOGRAPHIC rows, distinct nonnull PATIDs, missing PATID/birth dates, OMOP person rows, and OMOP person links to the source. A source-row denominator is distinct from unique persons.

## Safeguards

- SQL reads source and frozen target tables and creates only session-local temporary `#` tables. It does not update, delete, truncate, or create permanent study/ETL tables.
- It aborts before writing an output unless **D0 source = 9,815, OMOP = 6,001, shared = 6,001, source-only = 3,814, OMOP-only = 0**, and frozen OMOP person count is **27,089**. If these anchors fail, investigate the data snapshot and code version. Do **not** change the anchors to force success.
- Writes only aggregate JSON, with file mode `0600`, under the repository's ignored `results/` directory. Refuses to overwrite a prior output. Never commit or upload that JSON before institutional disclosure review.
- Performs preliminary suppression of small subgroup cells (<11), including complementary suppression of one additional cell when a partition contains exactly one small cell. **This does not replace institutional disclosure review** or prevent all differencing across multiple tables. Suppressed values and small count outputs must be screened further before release.
- Only offline Python/SQL-construction tests have run. The SQL has **not yet been executed or performance-tested against SQL Server**.

## Institutional execution (only when the approved execution path is available)

Copy `qiran_postfreeze_audit.py` into the checked-out project repository's `scripts/` folder **locally**. With the project's normal virtual environment activated and a read-only SQL Server user credential, run from the project root:

```bash
python scripts/qiran_postfreeze_audit.py --config config/etl_A.yaml \
  --output results/postfreeze_qiran/qiran_selected_d0_audit.json
```

The path `config/etl_A.yaml` is an example from the historical project; use the local active configuration if different. Do not paste credentials into commands, chat, notebooks, or version control.

After a successful run, a local reviewer must check source coverage, D0 anchors, subgroup and missingness denominators, small-cell risk, and the duplicate matching definition. Only **approved aggregate statistics** should be exported for manuscript integration.

## Suggested table design after verified execution

- **Main Results Stage C**: One sentence comparing source-only with shared D0 patient groups on the most consequential standardized differences; one sentence summarizing which years/coding eras have higher missing-date rates among primary qualifying stroke diagnosis records. Avoid causal explanations without evidence.
- **Supplement Table S21**: D0 source-only versus shared (n; age mean/SD; female proportion; length of stay; prior-year encounters; index-year bands; encounter type; source-only minus shared SMD). Specify D0 denominator.
- **Supplement Table S22**: qualifying primary-stroke diagnosis *record-level* missing DX_DATE by admission-year band and ICD code era (n, missing n, proportion). Show D0 index patient distribution separately, not as the denominator for source record missingness.
- **Supplement Table S23 (verification)**: aggregate source cross-stream exact-key overlap, eligible-record and lineage context, and a clear statement that overlap does not prove duplication or ETL error.
- **Supplement Table S24**: source DEMOGRAPHIC rows / unique PATIDs / target person / source-target linkage. Explain exclusions for missing demographics and birth date when confirmed.

## Open interpretation gates

- Does the missing-date pattern actually vary with calendar/coding era? It is a hypothesis until this query runs.
- Are there meaningful SMD differences in age, sex, length of stay, or utilization? Not known yet.
- Does exact source-event overlap represent true duplicate clinical assertions? Follow-up verification is required before labeling it a defect.
- The existing end-to-end risk differences remain descriptive, and no new confidence intervals have been generated. Their inferential target should be defined separately if added.

## Verification record

Offline syntax compilation and six pytest tests passed locally. These check the frozen anchor guard, subcategory suppression behavior, SQL code construction, safe identifier/output paths, and record-vs-patient denominator separation. They **do not** establish successful database execution or scientific results.
