# Publication analysis workflow

## Objective

Use the frozen audited PCORnet-to-OMOP ETL as a fixed input and quantify how clinically meaningful information and analytic results differ between PCORnet and OMOP after transformation.

Frozen ETL reference: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`.

This workflow is intentionally downstream of the ETL freeze. Analysis findings must not be used to tune ETL mappings solely to improve concordance.

## Branching model

- ETL freeze point: `audit/etl-freeze-candidate` at the frozen SHA above.
- Publication work: `publication/analysis`.
- Study-analysis commits may advance on `publication/analysis` while the ETL freeze SHA remains fixed in manifests and methods documentation.

## Analysis stages

### A. Structural and semantic concordance

Purpose: describe what the ETL does before asking whether clinical cohorts agree.

Primary outputs:

- source rows and eligible rows by source table;
- quantified exclusions by rule;
- source-event to route-row reconciliation;
- route-row to target-event reconciliation;
- one-to-many expansion by source and target domain;
- direct Standard vs `Maps to` vs unresolved routing;
- cross-domain route distributions;
- concept-0 counts/rates by domain and reason;
- unit and drug-route mapping coverage;
- visit linkage rates;
- lineage completeness;
- explicit non-event semantic components retained in ledgers.

Recommended manuscript artifacts:

- Table 1: source and frozen OMOP domain inventory;
- Table 2: source-to-target routing/reconciliation by source table;
- Table 3: unresolved/concept-0 and mapping-coverage summary;
- Figure 1: source -> route -> OMOP domain flow diagram;
- Figure 2: cross-domain routing proportions.

Acceptance rule: this stage is descriptive. Counts are not judged against the historical comparator database.

### B. Patient-level semantic concordance

Purpose: compare matched clinical facts at the patient/event level while respecting the representation rules of each CDM.

Candidate domains:

- encounters/visits;
- diagnoses/conditions;
- procedures;
- medications;
- measurements/labs/vitals;
- observations;
- death.

For each domain, define before analysis:

1. the PCORnet source definition;
2. the OMOP concept/domain definition;
3. the patient identifier linkage;
4. the time window/tolerance, if any;
5. whether one-to-many mappings count as concordant;
6. how concept `0` is handled;
7. how cross-domain mappings are handled.

Recommended metrics:

- patient prevalence in each CDM;
- absolute and relative prevalence difference;
- intersection/union size;
- Jaccard similarity;
- positive agreement;
- negative agreement where a defensible denominator exists;
- event-count correlation or paired difference where meaningful;
- temporal agreement for matched events;
- discordance decomposition by ETL route reason.

### C. Phenotype reproducibility

Purpose: test whether complete computable phenotypes identify the same patients after transformation.

The repository already contains stroke-focused study modules that can serve as the first phenotype family:

- `src/pcornet_omop_validation/study/stroke_codes.py`
- `src/pcornet_omop_validation/study/pcornet_stroke_phenotypes.py`
- `src/pcornet_omop_validation/study/stroke_d0_reconciliation.py`
- `src/pcornet_omop_validation/study/stroke_planning_audit.py`

Before comparing results, lock a phenotype specification containing:

- inclusion/exclusion criteria;
- diagnosis/procedure/lab code sets;
- age definition;
- encounter setting;
- temporal windows;
- index-event selection;
- follow-up requirements;
- outcome definitions;
- handling of missing/unmapped concepts.

Do not derive code-list changes from observed PCORnet-versus-OMOP disagreement unless the change corrects an independently demonstrated phenotype-specification error.

Primary phenotype outputs:

- cohort size in PCORnet;
- cohort size in frozen OMOP;
- shared patients;
- PCORnet-only patients;
- OMOP-only patients;
- Jaccard similarity;
- positive agreement;
- discordance reason categories;
- sensitivity analyses specified before outcome comparison.

### D. Analytical equivalence

Purpose: determine whether downstream scientific conclusions remain stable despite representation differences.

Candidate analyses should be selected and pre-specified before inspecting model differences. Depending on the final manuscript design, these may include:

- descriptive event rates;
- risk-factor associations;
- logistic regression;
- survival/time-to-event analyses;
- subgroup estimates;
- prediction-model performance;
- calibration/discrimination comparisons.

For every analysis, keep identical where possible:

- cohort definition;
- index date;
- covariate definitions;
- outcome definition;
- censoring rules;
- follow-up window;
- model specification;
- missing-data policy;
- subgroup definitions.

Report both numerical and inferential agreement; do not reduce equivalence to whether p-values cross 0.05.

## Reproducibility requirements

Each publication analysis run should record:

- frozen ETL SHA;
- analysis-code SHA;
- configuration path/hash with secrets excluded;
- study-definition/code-list version;
- database/schema identifiers;
- run timestamp;
- input audit/freeze-manifest hash where practical;
- output filenames and SHA-256 hashes;
- row/patient counts sufficient to reproduce each table/figure;
- software/runtime versions where relevant.

Generated row-level data should remain outside Git. Only code, non-sensitive aggregate summaries, and publication-safe metadata should be committed.

## Output layout

Recommended local layout:

```text
results/
  etl_audit_A/                 # frozen ETL audit bundle
  publication_analysis/
    manifests/
    stage_a_structural/
    stage_b_patient_concordance/
    stage_c_phenotypes/
    stage_d_analytics/
    manuscript_tables/
    manuscript_figures/
```

`results/` remains ignored by Git; publication-safe aggregate tables can later be copied deliberately into a reviewed manuscript-output directory if needed.

## Immediate implementation order

1. Create an analysis-run manifest helper anchored to frozen ETL SHA `887e6f4...`.
2. Build Stage A from existing ETL audit JSON, route ledgers, xwalks, and final target counts.
3. Produce one machine-readable Stage A summary and one human-readable table bundle.
4. Lock the first stroke phenotype specification.
5. Run source-only PCORnet phenotype generation.
6. Implement the same phenotype against frozen OMOP without consulting disagreement cases.
7. Reconcile patient membership and classify discordance.
8. Only after phenotype definitions are locked, proceed to outcome/analytical comparisons.

## Decision log for scientific changes

Any change to a phenotype, concordance rule, or statistical model after results have been inspected should be recorded with:

- what changed;
- why it changed;
- whether the change was motivated by a discovered bug, a pre-specified sensitivity analysis, or observed disagreement;
- which analyses were rerun.

This is intended to separate legitimate methodological refinement from post hoc optimization toward concordance.

## Manuscript linkage

The paper should keep ETL validation and scientific comparison distinct:

- Methods section 1: audited ETL and freeze protocol;
- Results section 1: ETL reconciliation/semantic validation;
- Methods section 2: concordance/phenotype/analytic study design;
- Results section 2: PCORnet-versus-OMOP scientific findings.

The ETL freeze record and Stage A results establish that later differences are being studied on top of a validated transformation rather than an uncontrolled historical converter.
