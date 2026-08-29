from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcornet_omop_validation.etl.config import load_etl_config

FROZEN_ETL_SHA = "887e6f4d60a6b185e58b3c9fe8887472b49777e3"


def _load(path: Path, status: str | None = None) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Missing required Stage C artifact: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("frozen_etl_sha") != FROZEN_ETL_SHA:
        raise RuntimeError(f"Artifact is not anchored to frozen ETL SHA: {path}")
    if status is not None and payload.get("status") != status:
        raise RuntimeError(f"Unexpected status in {path}: {payload.get('status')}")
    return payload


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _primary_row(phenotype: str, primary: dict[str, Any], portable: dict[str, Any]) -> dict[str, object]:
    return {
        "phenotype": phenotype,
        "source_patients": int(primary["source_patients"]),
        "lineage_faithful_omop_patients": int(primary["omop_patients"]),
        "intersection_patients": int(primary["intersection_patients"]),
        "source_only_patients": int(primary["source_only_patients"]),
        "omop_only_patients": int(primary["omop_only_patients"]),
        "patient_jaccard": float(primary["patient_jaccard"]),
        "positive_agreement_percent": float(primary["positive_agreement_percent"]),
        "exact_index_date_percent_among_shared": float(primary["exact_index_date_percent_among_shared"]),
        "native_omop_patients": int(portable["native_omop_patients"]),
        "native_omop_intersection_patients": int(portable["intersection_patients"]),
        "native_omop_source_only_patients": int(portable["source_only_patients"]),
        "native_omop_only_patients": int(portable["native_only_patients"]),
        "native_omop_patient_jaccard": float(portable["patient_jaccard"]),
        "native_omop_positive_agreement_percent": float(portable["positive_agreement_percent"]),
    }


def run(config_path: str) -> dict[str, Any]:
    cfg = load_etl_config(config_path)
    stage_root = cfg.audit_dir.parent / "publication_analysis" / "stage_c_phenotypes"
    d0_root = stage_root / "stroke_d0"
    d13_root = stage_root / "stroke_d1_d3"

    d0 = _load(d0_root / "stage_c_stroke_d0_final_summary.json", "stage_c_stroke_d0_manuscript_tables_complete")
    d13 = _load(d13_root / "stage_c_stroke_d1_d3_concordance.json", "stage_c_stroke_d1_d3_concordance_complete")
    audit = _load(
        d13_root / "stage_c_stroke_d1_d3_post_outcome_mechanism_audit.json",
        "stage_c_stroke_d1_d3_post_outcome_mechanism_audit_complete",
    )

    rows = [
        _primary_row("D0", d0["primary_transformation_fidelity"], d0["secondary_native_omop_portability"]),
        _primary_row("D1", d13["D1"]["primary_transformation_fidelity"], d13["D1"]["secondary_native_omop_portability"]),
        _primary_row("D3", d13["D3"]["primary_transformation_fidelity"], d13["D3"]["secondary_native_omop_portability"]),
    ]

    checks: dict[str, bool] = {}
    for row in rows:
        p = row["phenotype"]
        checks[f"{p}_source_partition_closes"] = row["intersection_patients"] + row["source_only_patients"] == row["source_patients"]
        checks[f"{p}_omop_partition_closes"] = row["intersection_patients"] + row["omop_only_patients"] == row["lineage_faithful_omop_patients"]
        checks[f"{p}_portable_source_partition_closes"] = row["native_omop_intersection_patients"] + row["native_omop_source_only_patients"] == row["source_patients"]
        checks[f"{p}_portable_omop_partition_closes"] = row["native_omop_intersection_patients"] + row["native_omop_only_patients"] == row["native_omop_patients"]

    mechanism_rows: list[dict[str, object]] = []
    for phenotype in ("D1", "D3"):
        m = audit[phenotype]["source_only_mechanism"]
        flags = audit[phenotype]["interpretation_flags"]
        mechanism_rows.append({
            "phenotype": phenotype,
            "source_only_patients": int(m["source_only_patients"]),
            "dx_date_null": int(m["dx_date_null"]),
            "dx_date_nonnull": int(m["dx_date_nonnull"]),
            "visit_xwalk_missing": int(m["visit_xwalk_missing"]),
            "diagnosis_xwalk_missing": int(m["diagnosis_xwalk_missing"]),
            "condition_row_missing_after_xwalk": int(m["condition_row_missing_after_xwalk"]),
            "lineage_base_missing": int(m["lineage_base_missing"]),
            "all_source_only_have_null_selected_dx_date": bool(flags["all_source_only_have_null_selected_dx_date"]),
            "all_source_only_missing_diagnosis_lineage": bool(flags["all_source_only_missing_diagnosis_lineage"]),
        })
        checks[f"{phenotype}_post_outcome_audit_reproduces_completed_concordance"] = all(audit[phenotype]["reproduction_checks"].values())

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"Combined Stage C phenotype summary invariants failed: {failed}")

    summary = {
        "status": "stage_c_stroke_phenotype_summary_complete",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_etl_sha": FROZEN_ETL_SHA,
        "all_invariants_matched": True,
        "checks": checks,
        "phenotype_rows": rows,
        "post_outcome_d1_d3_mechanism_rows": mechanism_rows,
        "analysis_roles": {
            "D0": "prespecified primary transformation-fidelity plus secondary native-OMOP portability",
            "D1_D3": "prespecified primary transformation-fidelity plus secondary native-OMOP portability",
            "D1_D3_mechanism_audit": "post-outcome diagnostic; explanatory only; must not redefine the frozen primary estimand",
        },
        "interpretation_guardrails": [
            "Lineage-faithful OMOP is the primary transformation-fidelity estimand.",
            "Native OMOP is a secondary portability sensitivity and intentionally does not restore non-native PCORnet semantics such as PDX.",
            "The D1/D3 mechanism audit was designed after observing completed D1/D3 concordance and is explanatory, not prespecified confirmatory analysis.",
            "No ETL, phenotype definition, evidence window, code list, age rule, or cohort ordering rule is changed by this summary.",
        ],
        "disclosure_review": {
            "aggregate_only_outputs": True,
            "patient_identifiers_written": False,
            "source_record_identifiers_written": False,
            "row_level_phi_written": False,
            "status": "passed",
        },
    }

    out_json = stage_root / "stage_c_stroke_phenotype_summary.json"
    out_csv = stage_root / "stage_c_stroke_phenotype_summary.csv"
    out_mech_csv = stage_root / "stage_c_stroke_d1_d3_mechanism_summary.csv"
    out_md = stage_root / "stage_c_stroke_phenotype_summary.md"

    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(out_csv, rows)
    _write_csv(out_mech_csv, mechanism_rows)

    lines = [
        "# Stage C ischemic-stroke phenotype reproducibility summary",
        "",
        f"Frozen ETL SHA: `{FROZEN_ETL_SHA}`",
        "",
        "## Primary transformation fidelity and secondary native-OMOP portability",
        "",
        "| Phenotype | PCORnet | Lineage OMOP | Shared | PCORnet only | OMOP only | Jaccard | Positive agreement | Exact shared index date | Native OMOP | Native shared | Native Jaccard |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r['phenotype']} | {r['source_patients']:,} | {r['lineage_faithful_omop_patients']:,} | {r['intersection_patients']:,} | {r['source_only_patients']:,} | {r['omop_only_patients']:,} | {r['patient_jaccard']:.6f} | {r['positive_agreement_percent']:.3f}% | {r['exact_index_date_percent_among_shared']:.3f}% | {r['native_omop_patients']:,} | {r['native_omop_intersection_patients']:,} | {r['native_omop_patient_jaccard']:.6f} |"
        )

    lines += [
        "",
        "## Post-outcome D1/D3 mechanism audit",
        "",
        "This section is explanatory only. It was designed after the completed D1/D3 concordance and is not part of the prespecified primary estimand.",
        "",
        "| Phenotype | Source only | DX_DATE null | DX_DATE nonnull | Visit xwalk missing | Diagnosis xwalk missing | Condition row missing after xwalk | Lineage base missing |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in mechanism_rows:
        lines.append(
            f"| {r['phenotype']} | {r['source_only_patients']:,} | {r['dx_date_null']:,} | {r['dx_date_nonnull']:,} | {r['visit_xwalk_missing']:,} | {r['diagnosis_xwalk_missing']:,} | {r['condition_row_missing_after_xwalk']:,} | {r['lineage_base_missing']:,} |"
        )

    lines += [
        "",
        "## Interpretation guardrails",
        "",
        "The lineage-faithful OMOP comparison remains the primary transformation-fidelity estimand. The native-OMOP comparison is a portability sensitivity. The D1/D3 mechanism audit is post-outcome diagnostic work and may explain observed discordance but cannot be used to redefine the frozen phenotype or ETL.",
        "",
        "Outputs are aggregate only. No patient-level or source-record-level identifiers are written.",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("status: stage_c_stroke_phenotype_summary_complete")
    print(f"all_invariants_matched: {summary['all_invariants_matched']}")
    for r in rows:
        print(f"{r['phenotype']}_source_patients: {r['source_patients']}")
        print(f"{r['phenotype']}_lineage_omop_patients: {r['lineage_faithful_omop_patients']}")
        print(f"{r['phenotype']}_patient_jaccard: {r['patient_jaccard']}")
    print(f"summary_json: {out_json}")
    print(f"summary_csv: {out_csv}")
    print(f"summary_md: {out_md}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build combined Stage C ischemic-stroke D0/D1/D3 phenotype summary")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
