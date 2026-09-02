# Stage E statistical and model reproducibility completion record

_Last updated: 2026-09-02_

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Study definition: `study_definitions/stage_e_statistical_model_reproducibility_v1.json`

Stage E is complete as a post-freeze extension beyond analytically closed Stages A-D.

## Provenance

- Outcome/model-free preflight SHA: `0ee2d0bb7c1b9123d58af59c5bfe8240ecd2271a`
- Initial Stage E implementation SHA: `f2b2c4a34c5811a0bd36d2ebaef865db8bfef9b0`
- Locked-D0 anchor correction SHA: `70f8fa61c62d8b577d74eeef1a6c8030942a7006`
- Completed analysis SHA: `bda49d3253dbd90cd390bc818e6953becfa743ec`
- Study-definition SHA-256: `5ca2fa4a08e33166e39c61f1893c06ea5fe3812e33a0ea0f98c15915493ae9a5`
- Inherited D0-definition SHA-256: `45c4fe08493e6a1713b2d1b38b6a6903e67784a1edcb5d01de119cccc3489418`
- Inherited Stage D-definition SHA-256: `e8a0914bee9993f34167ad2b336191f2a0c52e88e47adb99c9f2c94cd6f7abbf`

The first Stage E run stopped before descriptive or model outputs because the implementation reselected the earliest surviving OMOP stroke episode rather than anchoring to the already-selected source D0 episode. The correction changed only reconstruction of the inherited D0 anchor; the locked features, outcome, split, models, metrics, and interpretation hierarchy were unchanged.

## Locked anchor reproduction

Stage E reproduced all inherited Stage C/D anchors exactly:

| Metric | Count |
| --- | ---: |
| Source D0 | 9,815 |
| Lineage-faithful OMOP D0 | 6,001 |
| Source 90-day eligible | 6,508 |
| Source 90-day events | 1,798 |
| OMOP 90-day eligible | 3,822 |
| OMOP 90-day events | 1,132 |
| Fixed-cohort eligible | 3,822 |
| Fixed-cohort source events | 1,132 |
| Fixed-cohort OMOP events | 1,132 |

## Fixed-cohort descriptive reproducibility

Among the same 3,822 patients with the same index dates and outcome observability, independently constructed PCORnet and OMOP features were almost perfectly reproduced.

Exactly concordant patient-level features:

- age at index: 3,822/3,822 exact;
- female indicator: 3,822/3,822 exact;
- index length of stay: 3,822/3,822 exact;
- prior 365-day acute-care encounter count: 3,822/3,822 exact;
- prior 365-day ischemic-stroke indicator: 3,822/3,822 exact.

Prior 365-day all-encounter count was exact for 3,806/3,822 patients (99.58%). Its mean absolute difference was 0.00497 encounters, median absolute difference 0, Spearman correlation 0.999989, and standardized mean difference 0.00039.

Thus, feature construction was effectively identical for the fixed population; the small all-encounter discrepancy is negligible and does not materially alter distributions.

## Fixed-cohort association reproducibility

Multivariable logistic-regression coefficients and odds ratios were essentially identical between representations. For every prespecified predictor, the OMOP/source odds-ratio ratio was approximately 1.000; examples include:

- age: 1.000015;
- female indicator: 1.000019;
- index length of stay: 0.999949;
- prior acute-care encounter count: 1.000352;
- prior all-encounter count: 0.999409;
- prior ischemic stroke: 1.000241.

This supports near-exact reproducibility of conventional association estimates conditional on the same cohort.

## Fixed-cohort prediction reproducibility

### Logistic regression

Test set: 1,140 patients, 358 events in both representations.

- AUROC: source 0.591413, OMOP 0.591345; difference -0.000068.
- AUPRC: source 0.447941, OMOP 0.447823; difference -0.000118.
- Brier score: source 0.206220, OMOP 0.206229; difference +0.000009.
- Predicted-probability Pearson correlation: 0.999994.
- Spearman correlation: 0.999997.
- Mean absolute probability difference: 0.000084.
- Median absolute probability difference: 0.000039.
- Maximum absolute probability difference: 0.00755.

### Ridge logistic regression

Results were similarly near-identical:

- AUROC difference -0.000082.
- AUPRC difference -0.000098.
- Brier-score difference +0.000009.
- Predicted-probability Pearson correlation 0.999994.
- Mean absolute probability difference 0.000084.

### Histogram gradient boosting

Aggregate performance remained close but patient-level predictions were less identical than the logistic models:

- AUROC: source 0.598737, OMOP 0.597730; difference -0.00101.
- AUPRC difference -0.00427.
- Brier-score difference +0.000983.
- Predicted-probability Pearson correlation 0.9652.
- Spearman correlation 0.9494.
- Mean absolute probability difference 0.03698.
- Maximum absolute probability difference 0.2647.

The nonlinear model is therefore more sensitive to the small residual feature differences and tree/binning behavior even though aggregate discrimination and Brier score remain similar.

## End-to-end descriptive differences

When independently selected source and OMOP cohorts were used, differences emerged because the populations differed before modeling.

Selected standardized mean differences (source minus OMOP):

- age: -0.0159;
- female indicator: -0.0214;
- index length of stay: -0.0780;
- prior all-encounter count: -0.0583;
- prior acute-care encounter count: -0.1323;
- prior ischemic-stroke indicator: -0.1581.

The two largest prespecified distributional differences were therefore prior acute-care utilization and prior ischemic-stroke history, both exceeding the prespecified negligible threshold of |SMD|=0.1 but remaining below the moderate threshold of 0.2.

## End-to-end association and prediction differences

Association estimates remained generally similar, but no longer near-identical. Examples of OMOP/source odds-ratio ratios were:

- age 1.0026;
- female indicator 0.9817;
- length of stay 0.9670;
- prior acute-care count 1.0096;
- prior all-encounter count 0.9888;
- prior stroke 0.9961.

Prediction performance differed more visibly because the training and test populations differed.

For logistic regression:

- source AUROC 0.63463 versus OMOP 0.59134; difference -0.04328;
- source AUPRC 0.45477 versus OMOP 0.44782; difference -0.00695;
- source Brier score 0.19509 versus OMOP 0.20623; difference +0.01114;
- test prevalence 29.40% versus 31.40%.

Ridge logistic regression produced nearly the same pattern. Histogram gradient boosting showed an AUROC difference of -0.0290 and Brier-score difference of +0.0181.

These end-to-end differences must not be interpreted as a pure CDM-representation effect. They combine cohort selection, different empirical feature distributions, different training samples, and representation effects.

## Scientific interpretation

Stage E extends the Stage D conditional-versus-end-to-end distinction from simple outcome risks to multivariable statistical models.

When patient, index date, outcome, and observability are held fixed, independently constructed PCORnet and OMOP covariates are nearly identical; conventional logistic association estimates, discrimination, calibration, and individual predicted probabilities are correspondingly almost perfectly reproducible.

When the full CDM-specific cohorts are allowed to differ, population characteristics, fitted coefficients, model calibration/discrimination, and predicted risks can diverge even though the underlying mapped semantics and fixed-cohort features are highly preserved. The dominant explanation is therefore upstream cohort selection and the altered empirical population entering model development, not broad degradation of information after conversion.

The nonlinear gradient-boosting model shows that even very small feature-level differences can produce larger individual-level prediction differences than logistic models while retaining similar aggregate performance. This should be presented as model sensitivity, not as evidence of an ETL defect.

## Disclosure

All Stage E committed outputs are aggregate only. Patient identifiers, row-level predictions, row-level protected health information, and free-text clinical data were not written to committed outputs.

## Closure

Stage E should now be treated as complete for the prespecified v1 extension. Additional analyses should be reviewer-driven or motivated by an independently demonstrated methodological defect. Do not tune models to improve cross-CDM agreement.
