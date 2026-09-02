# Manuscript extension: harmonized Stage C sensitivity and Stage E statistical/model reproducibility

Frozen ETL SHA: `887e6f4d60a6b185e58b3c9fe8887472b49777e3`

This document extends the previously frozen manuscript through Stage D. It does not alter the locked Stage A-D analyses. It adds (1) a post-freeze harmonized `DX_DATE` Stage C sensitivity and (2) the prespecified Stage E statistical/model reproducibility extension.

## Methods addition: harmonized Stage C sensitivity

To distinguish phenotype discordance caused by asymmetric source-versus-target date eligibility from discordance caused by OMOP representation itself, we conducted a post-freeze sensitivity analysis in which the PCORnet phenotype was restricted to stroke diagnoses with nonmissing `DX_DATE`, matching the frozen ETL eligibility rule. The original source-faithful Stage C analysis remained unchanged and retained encounter-date fallback. D0, D1, and D3 were then reconstructed under symmetric nonmissing-`DX_DATE` eligibility and compared with lineage-faithful OMOP using the same locked imaging, lipid, age, and index-selection rules.

## Results addition: harmonized Stage C sensitivity

Under symmetric nonmissing-`DX_DATE` eligibility, phenotype reproducibility was exact. D0 contained 6,198 patients in both PCORnet and lineage-faithful OMOP; all 6,198 patients were shared and all selected index dates were identical. D1 contained 5,246 patients in both representations with complete overlap and exact index-date agreement. D3 contained 4,710 patients in both representations with complete overlap and exact index-date agreement. Patient Jaccard similarity was 1.000 for D0, D1, and D3.

This sensitivity establishes that the previously observed Stage C discordance was attributable to the asymmetric interaction between source encounter-date fallback and the ETL requirement for nonmissing `DX_DATE`, rather than an inability of OMOP to represent the retained stroke, imaging, or lipid evidence once eligibility was harmonized.

## Methods addition: Stage E statistical/model reproducibility

Stage E was prespecified after Stages A-D had been analytically closed and before any Stage E outcome/model query. The inherited outcome was the locked Stage D 90-day acute-care reutilization endpoint. Six core features were constructed independently from PCORnet and OMOP using conceptually matched native representations: age at index, female indicator, index length of stay, prior 365-day acute-care encounter count, prior 365-day all-encounter count, and prior 365-day ischemic-stroke indicator.

The primary Stage E estimand used the same 3,822 patients, index dates, outcomes, and observability in both representations while constructing features independently within each CDM. The secondary end-to-end estimand used representation-specific D0 cohorts and 90-day observability, allowing the known upstream cohort attrition to propagate into feature distributions and model development.

Descriptive reproducibility included missingness, means, standard deviations, medians, quartiles, standardized mean differences, and fixed-cohort patient-level agreement. Association reproducibility used multivariable logistic regression with all six core features. Prediction reproducibility used an effectively unpenalized logistic regression, ridge logistic regression, and histogram gradient boosting under a deterministic 70/30 patient-level split. Metrics included AUROC, AUPRC, Brier score, calibration intercept and slope, and, for the fixed cohort, patient-level predicted-probability agreement.

## Results addition: Stage E fixed-cohort reproducibility

The Stage E analysis reproduced all inherited Stage C/D anchors exactly: source D0 9,815; lineage-faithful OMOP D0 6,001; source 90-day eligible 6,508 with 1,798 events; OMOP 90-day eligible 3,822 with 1,132 events; and fixed cohort 3,822 with 1,132 events in both representations.

Among the fixed 3,822 patients, five of six features were identical for every patient: age, sex, index length of stay, prior acute-care encounter count, and prior ischemic-stroke indicator. Prior all-encounter count was exact for 3,806/3,822 patients (99.58%), with mean absolute difference 0.005 encounters, median absolute difference 0, Spearman correlation 0.999989, and standardized mean difference 0.00039.

Association estimates were correspondingly almost identical. The OMOP/source odds-ratio ratios for the six predictors ranged from 0.99941 to 1.00035.

Prediction-model reproducibility was also near-exact for the two logistic models. For ordinary logistic regression, AUROC was 0.591413 in PCORnet and 0.591345 in OMOP (difference -0.000068), AUPRC was 0.447941 versus 0.447823, and Brier score was 0.206220 versus 0.206229. Patient-level predicted probabilities had Pearson correlation 0.999994, Spearman correlation 0.999997, mean absolute difference 0.000084, and median absolute difference 0.000039. Ridge logistic regression showed essentially the same pattern.

Histogram gradient boosting retained close aggregate performance (AUROC difference -0.00101; Brier-score difference +0.00098) but individual predicted probabilities were less identical (Pearson correlation 0.965, Spearman 0.949, mean absolute difference 0.037). This indicates greater nonlinear-model sensitivity to small residual feature differences and binning/tree behavior even when aggregate discrimination remains similar.

## Results addition: Stage E end-to-end reproducibility

When independently selected source and OMOP cohorts were compared, population differences emerged. The standardized mean differences were -0.0159 for age, -0.0214 for female sex, -0.0780 for index length of stay, -0.0583 for prior all-encounter count, -0.1323 for prior acute-care encounter count, and -0.1581 for prior ischemic-stroke history. The latter two exceeded the prespecified negligible threshold of |0.1| but remained below the moderate threshold of |0.2|.

End-to-end association estimates remained broadly similar but were no longer numerically identical. Prediction performance differed more visibly. Ordinary logistic-regression AUROC was 0.63463 in PCORnet and 0.59134 in OMOP, a difference of -0.04328; AUPRC differed by -0.00695 and Brier score by +0.01114. Test-set outcome prevalence was 29.40% in PCORnet and 31.40% in OMOP. Ridge logistic regression showed an almost identical pattern. Histogram gradient boosting showed an AUROC difference of -0.0290 and Brier-score difference of +0.0181.

These end-to-end differences combine upstream cohort selection, changed empirical feature distributions, different training samples, and representation effects. They should not be attributed to the OMOP data model alone.

## Integrated interpretation through Stage E

The combined findings provide a layered explanation of apparent cross-CDM disagreement.

First, mapped clinical events and values are highly preserved after conversion. Second, a source phenotype that uses semantics unavailable to the frozen ETL—specifically encounter-date fallback for diagnoses with null `DX_DATE`—can yield a substantially different cohort. Third, when the source is subjected to the same nonmissing-`DX_DATE` rule, D0/D1/D3 phenotype reproduction becomes exact. Fourth, when the same patients and index dates are held fixed, downstream outcomes, covariates, association estimates, and conventional logistic predictions are essentially identical across CDMs. Fifth, when independently selected cohorts are used, descriptive characteristics and model performance can diverge because a different empirical population enters analysis.

The strongest manuscript-level conclusion is therefore:

**An audited PCORnet-to-OMOP transformation can preserve mapped semantics, complete harmonized phenotypes, fixed-cohort outcomes, covariates, association estimates, and conventional prediction models with near-exact fidelity, while still producing different end-to-end analytical results when source-specific phenotype semantics interact with ETL eligibility rules and thereby change the population entering analysis.**

## Suggested abstract addition

A post-freeze harmonized sensitivity requiring nonmissing diagnosis dates in both representations eliminated the previously observed stroke-phenotype discordance: D0, D1, and D3 were identical between PCORnet and lineage-faithful OMOP (Jaccard 1.000 with exact index dates). In a separately prespecified statistical/model extension, independently constructed covariates were nearly identical among 3,822 fixed-index patients, multivariable odds ratios were essentially unchanged, and logistic-model predicted probabilities correlated at 0.999994 between CDMs. In contrast, end-to-end logistic AUROC differed by 0.043 because the independently selected source and OMOP cohorts had different utilization and prior-stroke distributions. These findings localize major analytical divergence to cohort selection rather than general degradation of transformed clinical information.

## Reporting guardrails

- The harmonized Stage C analysis is a post-freeze sensitivity and does not replace the source-faithful primary Stage C estimand.
- Stage E is an extension beyond the analytically closed Stage A-D protocol and should be labeled as such.
- Similar prediction performance does not prove universal semantic equivalence.
- End-to-end model differences are not a pure CDM effect.
- The gradient-boosting patient-level probability differences should be interpreted as nonlinear model sensitivity, not evidence of an ETL defect.
