# Stage E statistical and model reproducibility plan

Stage E is a post-freeze extension beyond the analytically closed Stages A-D. It does not reopen or redefine any prior estimand.

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

Study definition: `study_definitions/stage_e_statistical_model_reproducibility_v1.json`

## Scientific question

Stage E asks whether transforming PCORnet to OMOP changes the statistical properties of an analysis even when mapped events are well preserved. It separates two estimands:

1. **Fixed-cohort representation reproducibility.** Hold patient, index date, and follow-up observability fixed, construct predictors independently from each CDM, and compare descriptive statistics, regression estimates, and prediction-model outputs.
2. **End-to-end reproducibility.** Construct the eligible source and OMOP cohorts independently, allowing the known Stage C cohort attrition and feature availability to affect the analysis.

## Outcome

The outcome remains the locked Stage D 90-day acute-care reutilization endpoint.

## Core predictors

- age at index;
- female indicator;
- index length of stay;
- prior 365-day acute-care encounter count;
- prior 365-day all-encounter count;
- prior 365-day ischemic-stroke indicator.

These were chosen because they are clinically interpretable, available in both representations, and do not require post-hoc feature selection based on cross-CDM agreement.

## Descriptive comparison

For continuous variables we will report sample size, missingness, mean, SD, median, IQR, range, and standardized mean difference. For binary variables we will report missingness, prevalence, and standardized mean difference. In the fixed cohort we will additionally compare patient-level feature agreement with absolute differences and correlations, while writing no row-level patient outputs.

## Association analysis

A multivariable logistic regression for the 90-day outcome will use the six locked predictors. Coefficients, odds ratios, confidence intervals, cross-CDM coefficient differences, and OMOP/source odds-ratio ratios will be reported.

## Prediction analyses

Three deliberately modest models are locked before execution:

- logistic regression;
- ridge logistic regression;
- histogram gradient boosting.

A deterministic SHA-256 patient hash will assign 70% train / 30% test. Shared fixed-cohort patients therefore receive exactly the same split in both representations. Outputs are aggregate only.

Metrics: AUROC, AUPRC, Brier score, calibration intercept, calibration slope, test prevalence, and—within the fixed cohort—agreement between patient-level predicted probabilities summarized by correlations and absolute differences.

## Interpretation

The primary Stage E result is the fixed-cohort comparison because it most directly tests whether independently constructed CDM representations change statistical inputs or model outputs. The end-to-end comparison is deliberately broader and includes upstream cohort-selection effects.

Stage E must not be used to retune the frozen ETL or prior phenotype definitions merely to improve model agreement.
