from __future__ import annotations

"""Generate the manuscript tables from frozen aggregate study results."""

import argparse
import csv
import json
from pathlib import Path


def _table(
    title: str,
    columns: list[str],
    rows: list[list[str]],
    note: str = "",
) -> dict:
    return {
        "title": title,
        "columns": columns,
        "rows": rows,
        "note": note,
    }


def main_tables(_: dict) -> dict[str, dict]:
    return {
        "Table1": _table(
            "Table 1 | Reproducibility across validation layers",
            [
                "Layer",
                "Comparison unit",
                "Primary evidence",
                "Result",
                "Interpretation",
            ],
            [
                [
                    "Structural transformation",
                    "Source eligibility/routing",
                    "Counts, exclusions, route ledgers",
                    "All major differences explained",
                    "No unexplained structural loss after freeze",
                ],
                [
                    "Mapped semantics",
                    "Patient-level mapped events/values",
                    "Exact canonical concordance",
                    "Exact across locked mapped denominators",
                    "High conditional semantic fidelity",
                ],
                [
                    "Stroke phenotype",
                    "Independently constructed D0/D1/D3",
                    "Membership, Jaccard, index dates",
                    "Primary Jaccard 0.608–0.622",
                    "Upstream eligibility changed cohort",
                ],
                [
                    "Harmonized sensitivity",
                    "Same diagnosis-date requirement in both representations",
                    "Membership and index dates",
                    "Jaccard 1.000; index agreement 100%",
                    "Divergence localized to diagnosis-date policy",
                ],
                [
                    "Acute-care outcomes",
                    "Same patient/index vs end-to-end",
                    "30/90-day risks",
                    "Fixed exact; end-to-end outside reproducibility margins",
                    "Outcome representation preserved; cohort selection changed estimate",
                ],
                [
                    "Statistical models",
                    "Features, associations, predictions",
                    "Fixed vs end-to-end analyses",
                    "Fixed near-identical; end-to-end diverged",
                    "Population shift propagated into inference/model performance",
                ],
            ],
            (
                "Mapped-event concordance and vocabulary coverage are intentionally "
                "reported separately."
            ),
        ),
        "Table2": _table(
            "Table 2 | Phenotype and downstream outcome reproducibility",
            [
                "Analysis",
                "PCORnet",
                "OMOP",
                "Shared/eligible",
                "Difference",
                "Agreement metric",
                "Conclusion",
            ],
            [
                [
                    "D0 source-faithful",
                    "9,815",
                    "6,001",
                    "6,001 shared",
                    "3,814 source-only",
                    "Jaccard 0.611",
                    "Different cohort",
                ],
                [
                    "D1 source-faithful",
                    "8,624",
                    "5,246",
                    "5,245 shared",
                    "3,379 source-only; 1 OMOP-only",
                    "Jaccard 0.608",
                    "Different cohort",
                ],
                [
                    "D3 source-faithful",
                    "7,565",
                    "4,710",
                    "4,709 shared",
                    "2,856 source-only; 1 OMOP-only",
                    "Jaccard 0.622",
                    "Different cohort",
                ],
                [
                    "D0/D1/D3 harmonized",
                    "6,198 / 5,246 / 4,710",
                    "Same",
                    "All shared",
                    "0",
                    "Jaccard 1.000; index 100%",
                    "Exact after applying the same diagnosis-date requirement",
                ],
                [
                    "90-day fixed-index risk",
                    "29.6%",
                    "29.6%",
                    "3,822 eligible",
                    "0.00 pp",
                    "RR 1.00",
                    "Within reproducibility margins",
                ],
                [
                    "90-day end-to-end risk",
                    "27.6%",
                    "29.6%",
                    "6,508 / 3,822 eligible",
                    "+1.99 pp",
                    "RR 1.07",
                    "Outside reproducibility margins",
                ],
            ],
            (
                "Prespecified empirical reproducibility margins were ±0.5 percentage "
                "points for risk difference and 0.95–1.05 for the OMOP/PCORnet risk "
                "ratio; these were operational cross-CDM tolerances, not clinical "
                "noninferiority margins."
            ),
        ),
        "Table3": _table(
            "Table 3 | Fixed-cohort versus end-to-end statistical reproducibility",
            [
                "Domain",
                "Fixed cohort",
                "End-to-end PCORnet",
                "End-to-end OMOP",
                "Interpretation",
            ],
            [
                [
                    "Population",
                    "n=3,822; same patients/index",
                    "n=6,508",
                    "n=3,822",
                    "End-to-end cohort differs upstream",
                ],
                [
                    "Largest feature imbalance",
                    "All |SMD|<0.001",
                    "Reference",
                    "Prior stroke |SMD|=0.16; prior acute care 0.13",
                    "Selective case-mix shift",
                ],
                [
                    "Logistic AUROC",
                    "0.59 vs 0.59; Δ<0.001",
                    "0.63",
                    "0.59",
                    "Fixed discrimination reproduced; end-to-end differs",
                ],
                [
                    "Logistic probability agreement",
                    "Pearson >0.999; MAD<0.001",
                    "Not same patient set",
                    "Not same patient set",
                    "Patient-level predictions reproduced when population fixed",
                ],
                [
                    "Gradient-boosting probability agreement",
                    "Pearson 0.965; MAD 0.037",
                    "—",
                    "—",
                    "Nonlinear model amplifies small feature differences",
                ],
                [
                    "End-to-end Brier score",
                    "—",
                    "0.20",
                    "0.21",
                    "Prediction error differs with population/model fit",
                ],
            ],
            (
                "The conventional |SMD|=0.10 value is used descriptively, not as a "
                "prespecified statistical threshold."
            ),
        ),
    }


def supplementary_tables(_: dict) -> dict[str, dict]:
    tables: dict[str, dict] = {}

    tables["S1"] = _table(
        "Supplementary Table S1 | Frozen OMOP target-domain counts",
        ["OMOP domain", "Rows"],
        [
            ["Person", "27,089"],
            ["Observation period", "27,087"],
            ["Visit occurrence", "1,510,957"],
            ["Condition occurrence", "7,315,572"],
            ["Procedure occurrence", "4,182,803"],
            ["Measurement", "85,715,435"],
            ["Observation", "7,319,081"],
            ["Drug exposure", "48,458,058"],
            ["Device exposure", "196,660"],
            ["Specimen", "93"],
            ["Death", "6,955"],
        ],
        (
            "Counts correspond to the frozen validated OMOP target used for publication "
            "analyses; exact OMOP schema identifiers are retained in the reproducible "
            "ETL code."
        ),
    )

    tables["S2"] = _table(
        "Supplementary Table S2 | Stage A exclusions and routing",
        [
            "Source area",
            "Source / eligible",
            "Excluded or alternate routing",
            "Primary reason / interpretation",
        ],
        [
            [
                "Diagnosis records",
                "11,484,577 source; 8,024,792 eligible",
                "3,459,785 excluded (30.1%)",
                "Missing diagnosis date",
            ],
            [
                "Procedure records",
                "11,244,947 source; 11,228,023 eligible",
                "16,924 excluded (0.2%)",
                "Missing procedure date",
            ],
            [
                "Procedure route ledger",
                "11,234,863 routes",
                "111,660 unresolved; 1,642 non-event semantic components",
                "Vocabulary/semantic routing separated from event routes",
            ],
            [
                "Diagnosis + condition records",
                "8,674,973 eligible source events",
                (
                    "9,045,157 canonical routes; 361,606 events >1 core route; "
                    "60,148 unmapped-concept fallback"
                ),
                "One-to-many canonical routing preserved",
            ],
            [
                "Clinical observation records",
                "38,850,928 rows",
                (
                    "37,327,978 Measurement; 1,471,098 Observation; 39,115 Condition; "
                    "12,737 unresolved/unmapped"
                ),
                "Routed by Standard OMOP concept domain",
            ],
            [
                "Drug route ledger",
                "48,457,880 routes",
                "17,469,480 unmapped-concept routes (36.1%)",
                "Mapping/vocabulary limitation, retained with source value",
            ],
        ],
    )

    tables["S3"] = _table(
        "Supplementary Table S3 | Stage B mapped semantic concordance",
        [
            "Domain",
            "Mapped comparison",
            "Concordance",
            "Separate coverage limitations",
        ],
        [
            [
                "Encounter",
                "1,510,957",
                "Exact; patient Jaccard 1.000",
                "None material",
            ],
            [
                "Death",
                "6,955",
                "Exact; patient Jaccard 1.000",
                "None material",
            ],
            [
                "Condition",
                "8,983,621 mapped source routes",
                "All exact",
                (
                    "60,148 unmapped-concept fallback; target excess explained by other "
                    "audited provenance"
                ),
            ],
            [
                "Procedure",
                "11,121,561 mapped source routes",
                "All exact",
                "111,660 unresolved + 1,642 non-event components; target excess explained",
            ],
            [
                "Drug",
                "30,988,400 mapped nonzero Standard Drug routes",
                "All exact; patient Jaccard 1.000",
                "17,469,480 unmapped-concept routes",
            ],
            [
                "Measurement / Observation",
                "92,668,145 mapped rows",
                "All exact; patient Jaccard 1.000",
                (
                    "366,371 unresolved/descriptive unmapped records excluded from mapped "
                    "comparison"
                ),
            ],
        ],
        (
            "Coverage limitations are shown separately so unresolved or unmapped records "
            "are not misclassified as failures among successfully mapped events."
        ),
    )

    tables["S4"] = _table(
        "Supplementary Table S4 | Stage B value-level concordance",
        ["Value type", "Comparable denominator", "Agreement", "Residual / explanation"],
        [
            [
                "Numeric",
                "75,769,622",
                "75,644,000 directly exact",
                (
                    "125,622 differences, all vital-sign records and fully explained by "
                    "the prespecified transformation logic; 0 unexplained"
                ),
            ],
            [
                "Units",
                "58,916,347 uniquely resolved active Standard UCUM rows",
                "100% agreement",
                "No residual disagreement",
            ],
            [
                "Categorical values",
                "809,630 mapped categorical concepts",
                "100% agreement",
                "No residual disagreement",
            ],
        ],
    )

    tables["S5"] = _table(
        "Supplementary Table S5 | Stage C primary source-faithful phenotype comparison",
        [
            "Phenotype",
            "PCORnet",
            "OMOP",
            "Shared",
            "Source-only",
            "OMOP-only",
            "Jaccard",
            "Exact index among shared",
        ],
        [
            ["D0", "9,815", "6,001", "6,001", "3,814", "0", "0.611", "100.0%"],
            ["D1", "8,624", "5,246", "5,245", "3,379", "1", "0.608", "97.4%"],
            ["D3", "7,565", "4,710", "4,709", "2,856", "1", "0.622", "97.5%"],
        ],
        (
            "All source-only D1/D3 patients had a selected diagnosis with a missing "
            "diagnosis date and lacked diagnosis lineage under the frozen transformation. "
            "Shared index-date mismatches arose from selection of another surviving episode."
        ),
    )

    tables["S6"] = _table(
        "Supplementary Table S6 | Stage C harmonized diagnosis-date sensitivity",
        [
            "Phenotype",
            "PCORnet",
            "OMOP",
            "Shared",
            "Source-only",
            "OMOP-only",
            "Jaccard",
            "Exact index",
        ],
        [
            ["D0", "6,198", "6,198", "6,198", "0", "0", "1.000", "100.0%"],
            ["D1", "5,246", "5,246", "5,246", "0", "0", "1.000", "100.0%"],
            ["D3", "4,710", "4,710", "4,710", "0", "0", "1.000", "100.0%"],
        ],
        (
            "Post-freeze sensitivity requiring a recorded diagnosis date in both PCORnet "
            "and OMOP; all other locked phenotype rules were unchanged."
        ),
    )

    tables["S7"] = _table(
        "Supplementary Table S7 | Stage D acute-care outcome reproducibility",
        [
            "Estimand",
            "Eligible PCORnet / OMOP",
            "Events PCORnet / OMOP",
            "Risk PCORnet / OMOP",
            "Abs diff, pp",
            "RR",
            "Reproducibility margin",
        ],
        [
            [
                "Fixed 30 day",
                "4,374 / 4,374",
                "753 / 753",
                "17.2% / 17.2%",
                "0.00",
                "1.00",
                "Met",
            ],
            [
                "Fixed 90 day",
                "3,822 / 3,822",
                "1,132 / 1,132",
                "29.6% / 29.6%",
                "0.00",
                "1.00",
                "Met",
            ],
            [
                "End-to-end 30 day",
                "7,277 / 4,374",
                "1,178 / 753",
                "16.2% / 17.2%",
                "+1.03",
                "1.06",
                "Not met",
            ],
            [
                "End-to-end 90 day",
                "6,508 / 3,822",
                "1,798 / 1,132",
                "27.6% / 29.6%",
                "+1.99",
                "1.07",
                "Not met",
            ],
        ],
        (
            "Prespecified empirical reproducibility margins: absolute risk difference "
            "±0.5 percentage points and RR 0.95–1.05. All 1,132 fixed 90-day "
            "both-positive patients had exactly matching first-event dates; median time "
            "to first event was 26 days in both representations."
        ),
    )

    tables["S8"] = _table(
        "Supplementary Table S8 | Stage D recurrent ischemic stroke analysis",
        [
            "Analysis",
            "Eligible",
            "PCORnet events",
            "OMOP events",
            "Agreement",
            "Discordance / attribution",
        ],
        [
            [
                "Primary recurrent stroke-code endpoint, days 31–365",
                "2,531",
                "263",
                "258",
                "2,526/2,531 (99.8%)",
                (
                    "5 source-only; visits/timing preserved but qualifying "
                    "diagnosis-to-condition lineage absent"
                ),
            ],
            [
                "Post-outcome principal-diagnosis sensitivity",
                "2,531",
                "170",
                "170",
                "2,531/2,531",
                "Complete agreement",
            ],
        ],
    )

    tables["S9"] = _table(
        "Supplementary Table S9 | Stage E fixed-cohort feature and association reproducibility",
        ["Feature", "Patient-level agreement", "Fixed SMD", "OMOP/source OR ratio"],
        [
            ["Age at index", "3,822/3,822 exact", "0.00", "1.000"],
            ["Female indicator", "3,822/3,822 exact", "0.00", "1.000"],
            ["Index length of stay", "3,822/3,822 exact", "0.00", "1.000"],
            [
                "Prior 365-day acute-care encounters",
                "3,822/3,822 exact",
                "0.00",
                "1.000",
            ],
            [
                "Prior 365-day all encounters",
                "3,806/3,822 exact; MAD 0.005; Spearman >0.999",
                "<0.001",
                "0.999",
            ],
            [
                "Prior 365-day ischemic stroke",
                "3,822/3,822 exact",
                "0.00",
                "1.000",
            ],
        ],
        (
            "Association estimates are from the prespecified multivariable logistic "
            "model. No formal coefficient-equivalence threshold was specified."
        ),
    )

    tables["S10"] = _table(
        "Supplementary Table S10 | Stage E end-to-end population characteristics",
        ["Feature", "PCORnet", "OMOP", "SMD (source−OMOP)"],
        [
            ["Age, mean years", "67.11", "67.34", "−0.02"],
            ["Female", "46.9%", "48.0%", "−0.02"],
            ["Index length of stay, mean days", "6.91", "7.68", "−0.08"],
            ["Prior acute-care encounters, mean", "0.75", "0.96", "−0.13"],
            ["Prior all encounters, mean", "6.77", "7.49", "−0.06"],
            ["Prior ischemic stroke", "6.3%", "10.7%", "−0.16"],
        ],
        (
            "Using the conventional descriptive heuristic |SMD|>0.10, the largest "
            "differences were prior ischemic-stroke history and prior acute-care "
            "utilization. These are descriptive population differences, not causal effects."
        ),
    )

    tables["S11"] = _table(
        "Supplementary Table S11 | Stage E prediction-model reproducibility",
        [
            "Model",
            "Fixed AUROC PCORnet / OMOP (Δ)",
            "Fixed probability agreement",
            "End-to-end AUROC PCORnet / OMOP (Δ)",
            "End-to-end Brier PCORnet / OMOP",
        ],
        [
            [
                "Logistic regression",
                "0.59 / 0.59 (Δ<0.001)",
                "Pearson >0.999; MAD <0.001",
                "0.63 / 0.59 (−0.04)",
                "0.20 / 0.21",
            ],
            [
                "Ridge logistic",
                "0.59 / 0.59 (Δ<0.001)",
                "Pearson >0.999; MAD <0.001",
                "0.63 / 0.59 (−0.04)",
                "0.20 / 0.21",
            ],
            [
                "Histogram gradient boosting",
                "0.60 / 0.60 (−0.001)",
                "Pearson 0.965; MAD 0.037",
                "0.63 / 0.60 (−0.03)",
                "0.20 / 0.22",
            ],
        ],
        (
            "Fixed-cohort comparisons hold patient, index date, observability, and "
            "outcome population constant while constructing features independently in "
            "each CDM. End-to-end comparisons permit the independently selected "
            "populations to differ."
        ),
    )

    tables["S12"] = _table(
        "Supplementary Table S12 | Stage E calibration metrics",
        [
            "Model",
            "Estimand",
            "Intercept PCORnet / OMOP",
            "Slope PCORnet / OMOP",
            "Test n / events PCORnet",
            "Test n / events OMOP",
        ],
        [
            [
                "Logistic",
                "Fixed",
                "0.04 / 0.04",
                "0.91 / 0.91",
                "1,140 / 358",
                "1,140 / 358",
            ],
            [
                "Logistic",
                "End-to-end",
                "0.17 / 0.04",
                "1.06 / 0.91",
                "1,939 / 570",
                "1,140 / 358",
            ],
            [
                "Ridge logistic",
                "Fixed",
                "0.04 / 0.04",
                "0.91 / 0.91",
                "1,140 / 358",
                "1,140 / 358",
            ],
            [
                "Ridge logistic",
                "End-to-end",
                "0.17 / 0.04",
                "1.06 / 0.91",
                "1,939 / 570",
                "1,140 / 358",
            ],
            [
                "Gradient boosting",
                "Fixed",
                "−0.41 / −0.44",
                "0.39 / 0.37",
                "1,140 / 358",
                "1,140 / 358",
            ],
            [
                "Gradient boosting",
                "End-to-end",
                "−0.30 / −0.44",
                "0.57 / 0.37",
                "1,939 / 570",
                "1,140 / 358",
            ],
        ],
        (
            "Ideal calibration is intercept 0 and slope 1. No formal "
            "calibration-equivalence margins were prespecified for Stage E v1."
        ),
    )

    tables["S13"] = _table(
        "Supplementary Table S13 | Implementation issues identified during PCORnet-to-OMOP ETL validation",
        [
            "Starting implementation issue / decision",
            "Why it matters",
            "Publication ETL approach",
            "Generalizable lesson",
        ],
        [
            [
                "Missing diagnosis dates replaced by 1900-01-01",
                "Converts missingness into apparently valid longitudinal information",
                "Exclude when target-required event date absent",
                (
                    "Avoid replacing missing clinical dates with plausible sentinel dates "
                    "unless they remain explicitly distinguishable from observed dates"
                ),
            ],
            [
                "PCORnet condition records absent from the starting condition-event conversion",
                "Omits a distinct source condition-provenance stream",
                "Include diagnosis and condition records with lineage",
                "Audit all source domains contributing to a target clinical domain",
            ],
            [
                "Vital-sign conversion file in the evaluated package was mispackaged or inconsistent with its filename",
                "Can omit vital data or duplicate another domain",
                (
                    "Restore domain-specific vital-sign conversion logic and validate "
                    "resulting values against source data"
                ),
                "Verify conversion scripts against source/target fields, not filenames alone",
            ],
            [
                "Observation-like source records require target-domain resolution",
                (
                    "Mapped Standard concepts can belong to Measurement, Observation, "
                    "Condition or other OMOP domains"
                ),
                "Route by Standard concept domain",
                (
                    "Route records according to the domain of the mapped Standard concept "
                    "rather than source-table identity alone"
                ),
            ],
            [
                "Valid one-to-many mappings can be lost if source rows are forced into one target representation",
                "Loses legitimate target semantics",
                "Preserve valid canonical routes",
                (
                    "Do not assume one source row must correspond to exactly one canonical "
                    "OMOP representation"
                ),
            ],
            [
                "Source events without a Standard concept mapping",
                (
                    "Dropping otherwise eligible unmapped events can create hidden "
                    "information loss or selection"
                ),
                (
                    "Retain the event using OMOP's unmapped concept where permitted and "
                    "preserve source-code information"
                ),
                "Separate mapping coverage from event preservation",
            ],
            [
                "Procedures with a missing procedure date",
                "The target procedure event requires a valid event date",
                "Exclude and explicitly count",
                (
                    "Quantify exclusions caused by target-required fields and test their "
                    "analytical consequences"
                ),
            ],
            [
                "Multiple drug source pathways",
                (
                    "Prescribing, medication-administration, and dispensing records can "
                    "differ in vocabulary-mapping and encounter-linkage pathways"
                ),
                "Preserve pathway/source lineage",
                (
                    "Validate drug source pathways separately before interpreting pooled "
                    "drug-exposure results"
                ),
            ],
            [
                "Provider data unavailable in the source extract",
                (
                    "Provider entities and downstream provider linkages cannot be "
                    "reconstructed completely"
                ),
                "Treat as explicit source limitation",
                (
                    "Treat unavailable source provenance as an explicit limitation rather "
                    "than synthesizing unsupported mappings"
                ),
            ],
        ],
        (
            "These items describe issues and design decisions identified in the conversion "
            "package and source extract evaluated in this study. They should not be "
            "interpreted as deficiencies of the PCORnet or OMOP data models themselves, "
            "nor as claims about the current state of any external community-maintained "
            "converter. The publication ETL refers to the frozen transformation used for "
            "the analyses reported in this study."
        ),
    )

    tables["S14"] = _table(
        "Supplementary Table S14 | Locked ischemic-stroke phenotype definitions",
        ["Component", "D0", "D1", "D3"],
        [
            [
                "Base stroke episode",
                (
                    "Adult ED-to-inpatient or inpatient encounter; ≥1 calendar-day "
                    "overnight stay; exact locked ischemic-stroke ICD-9-CM/ICD-10-CM "
                    "code; principal diagnosis"
                ),
                "Same as D0",
                "Same as D0",
            ],
            [
                "Imaging requirement",
                "None",
                "≥1 CT or MRI procedure",
                "≥1 MRI procedure",
            ],
            [
                "Imaging codes / window",
                "—",
                (
                    "CT CPT 70450, 70460, 70470 or MRI CPT 70551, 70552, 70553, "
                    "70557, 70558, 70559; procedure date from admission−2 days through "
                    "discharge"
                ),
                (
                    "MRI CPT 70551, 70552, 70553, 70557, 70558, 70559; procedure date "
                    "from admission−2 days through discharge"
                ),
            ],
            [
                "Lipid requirement",
                "None",
                "≥1 result with LOINC in versioned locked whitelist during admission–discharge",
                "Same as D1",
            ],
            [
                "Index selection",
                (
                    "First qualifying encounter per patient; source index uses diagnosis "
                    "date when available, otherwise encounter dates"
                ),
                "Imaging/lipid evaluated per D0-eligible encounter before first-event ranking",
                "Same as D1",
            ],
            ["Age rule", "After first qualifying encounter, age ≥18 years", "Same", "Same"],
            [
                "Primary source-date behavior",
                (
                    "Selected diagnosis ordered by diagnosis date with missing dates last; "
                    "source index can fall back to encounter dates"
                ),
                "Inherited from D0",
                "Inherited from D0",
            ],
        ],
        (
            "D1 is D0 plus CT-or-MRI imaging and lipid evidence; D3 is D0 plus MRI "
            "imaging and lipid evidence. The primary comparison preserved source-faithful "
            "rules; the post-freeze harmonized sensitivity additionally required a recorded "
            "diagnosis date in both representations without changing other rules. Exact "
            "field and encounter-type codes are retained in the versioned study definitions "
            "and analysis code."
        ),
    )

    return tables


def export_all(data_path: str | Path, outdir: str | Path) -> dict[str, dict]:
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    output_dir = Path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)

    specs = {
        **main_tables(data),
        **supplementary_tables(data),
    }
    (output_dir / "table_specs.json").write_text(
        json.dumps(specs, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    for key, table in specs.items():
        with (output_dir / f"{key}.csv").open(
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(table["columns"])
            writer.writerows(table["rows"])

    return specs


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate canonical publication tables")
    parser.add_argument(
        "--data",
        default="study_definitions/artifacts/publication_figure_data_v1.json",
    )
    parser.add_argument(
        "--outdir",
        default="results/publication_assets/tables",
    )
    args = parser.parse_args()
    export_all(args.data, args.outdir)


if __name__ == "__main__":
    main()
