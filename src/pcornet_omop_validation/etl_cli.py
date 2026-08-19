from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pcornet_omop_validation.etl import (
    apply_omop_schema,
    audit_condition_mapping,
    load_etl_config,
    load_pcornet_staging,
    load_vocabulary,
    run_preflight,
    transform_condition_occurrence,
    transform_observation_period,
    transform_person,
    transform_visit_occurrence,
    write_run_manifest,
)
from pcornet_omop_validation.etl.athena import acquire_athena_vocabulary
from pcornet_omop_validation.etl.config import save_etl_config
from pcornet_omop_validation.etl.decisions import (
    prompt_for_decisions,
    unresolved_decisions,
    validate_decisions,
    write_decision_log,
)
from pcornet_omop_validation.etl.dependencies import acquire_public_dependencies


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcornet-omop-etl",
        description="Audited PCORnet-to-OMOP ETL runner (v1 foundation).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Print the configured ETL stage order")
    plan.add_argument("--config", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="Validate source files, vocabulary files, and required secrets"
    )
    preflight.add_argument("--config", required=True)
    preflight.add_argument("--json", action="store_true", dest="as_json")

    acquire = subparsers.add_parser(
        "acquire",
        help="Acquire pinned OHDSI assets and an authorized Athena vocabulary bundle",
    )
    acquire.add_argument("--config", required=True)

    configure = subparsers.add_parser(
        "configure",
        help="Interactively resolve scientific/ETL policy decisions and record them",
    )
    configure.add_argument("--config", required=True)

    manifest = subparsers.add_parser(
        "manifest", help="Write the current ETL configuration/provenance manifest"
    )
    manifest.add_argument("--config", required=True)

    schema = subparsers.add_parser(
        "schema",
        help="Create the isolated target database if needed and apply pinned OHDSI OMOP DDL",
    )
    schema.add_argument("--config", required=True)

    vocabulary = subparsers.add_parser(
        "vocabulary",
        help="Bulk-load and reconcile Athena vocabulary tables into the OMOP target",
    )
    vocabulary.add_argument("--config", required=True)

    staging = subparsers.add_parser(
        "staging",
        help="Load PCORnet parquet files into audited SQL Server staging tables",
    )
    staging.add_argument("--config", required=True)

    person = subparsers.add_parser(
        "person",
        help="Transform PCORnet DEMOGRAPHIC into audited OMOP person records",
    )
    person.add_argument("--config", required=True)

    observation_period = subparsers.add_parser(
        "observation-period",
        help="Transform PCORnet ENROLLMENT into audited OMOP observation_period records",
    )
    observation_period.add_argument("--config", required=True)

    visit_occurrence = subparsers.add_parser(
        "visit-occurrence",
        help="Transform PCORnet ENCOUNTER into audited OMOP visit_occurrence records",
    )
    visit_occurrence.add_argument("--config", required=True)

    condition_occurrence = subparsers.add_parser(
        "condition-occurrence",
        help="Transform PCORnet DIAGNOSIS and CONDITION into audited OMOP condition_occurrence records",
    )
    condition_occurrence.add_argument("--config", required=True)

    condition_mapping_audit = subparsers.add_parser(
        "condition-mapping-audit",
        help="Decompose condition concept mapping outcomes without modifying OMOP records",
    )
    condition_mapping_audit.add_argument("--config", required=True)

    return parser


def _decision_log_path(config) -> Path:
    configured = config.raw.get("output", {}).get("decision_log")
    return Path(configured) if configured else config.audit_dir / "decisions.yaml"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_etl_config(args.config)

    if args.command == "plan":
        print(f"OMOP CDM version: {config.raw['etl']['cdm_version']}")
        print(f"Backend: {config.raw['etl']['backend']}")
        print(f"Target database: {config.raw['sqlserver']['database']}")
        print("Stages:")
        for index, stage in enumerate(config.stages, start=1):
            print(f"  {index:02d}. {stage}")
        pending = unresolved_decisions(config.raw)
        print(f"Unresolved policy decisions: {len(pending)}")
        for spec in pending:
            print(f"  - {spec.key}: {spec.title}")
        return 0

    if args.command == "acquire":
        try:
            assets = acquire_public_dependencies(config)
            for asset in assets:
                print(f"Acquired {asset.name} {asset.version}")
                print(f"  archive: {asset.archive}")
                print(f"  sha256:  {asset.sha256}")
            if assets:
                print(f"Dependency manifest: {config.audit_dir / 'dependencies.json'}")

            athena = acquire_athena_vocabulary(config)
            if athena is None:
                print(f"Athena vocabulary already available: {config.vocabulary_dir}")
            else:
                print("Acquired and validated Athena vocabulary bundle")
                print(f"  directory: {athena.directory}")
                print(f"  sha256:    {athena.sha256}")
                print(f"  manifest:  {config.audit_dir / 'athena_vocabulary.json'}")
            return 0
        except Exception as exc:
            print(f"ERROR: dependency acquisition failed: {exc}")
            return 2

    if args.command == "configure":
        errors = validate_decisions(config.raw)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 2

        selected = prompt_for_decisions(config.raw)
        if not selected:
            print("All configured ETL policy decisions are already resolved.")
            return 0

        policies = config.raw.setdefault("policies", {})
        policies.update(selected)
        save_etl_config(config)
        write_decision_log(
            _decision_log_path(config),
            config_path=config.path,
            decisions=selected,
            source="interactive",
        )
        print(f"Updated {config.path}")
        print(f"Recorded decisions in {_decision_log_path(config)}")
        return 0

    if args.command == "manifest":
        path = write_run_manifest(config)
        print(f"Run manifest: {path}")
        return 0

    if args.command == "schema":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = apply_omop_schema(config)
        except Exception as exc:
            print(f"ERROR: schema stage failed: {exc}")
            return 2
        write_run_manifest(config, status="schema_ready")
        print(f"Target database: {config.raw['sqlserver']['database']}")
        print(f"Database created: {'yes' if result.database_created else 'no'}")
        print(f"DDL: {result.ddl_path}")
        if result.already_present:
            print("OMOP schema already present; no DDL was re-applied.")
        else:
            print(f"Applied {result.batches_executed} SQL batch(es).")
        return 0

    if args.command == "vocabulary":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = load_vocabulary(config)
        except Exception as exc:
            print(f"ERROR: vocabulary stage failed: {exc}")
            return 2
        write_run_manifest(config, status="vocabulary_ready")
        print(f"Vocabulary load complete: {len(result.tables)} table(s)")
        print(f"Target: {result.database}.{result.schema}")
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "staging":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = load_pcornet_staging(config)
        except Exception as exc:
            print(f"ERROR: staging stage failed: {exc}")
            return 2
        write_run_manifest(config, status="staging_ready")
        print(f"Staging load complete: {len(result.tables)} table(s)")
        print(f"Target: {result.database}.{result.schema}")
        if result.missing_optional:
            print("Optional source tables absent: " + ", ".join(result.missing_optional))
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "person":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = transform_person(config)
        except Exception as exc:
            print(f"ERROR: person stage failed: {exc}")
            return 2
        write_run_manifest(config, status="person_ready")
        print(f"Person source rows: {result.source_rows:,}")
        print(f"Eligible rows: {result.eligible_rows:,}")
        print(f"Excluded rows: {result.excluded_rows:,}")
        print(f"  missing PATID: {result.excluded_missing_patid:,}")
        print(f"  missing BIRTH_DATE: {result.excluded_missing_birth_date:,}")
        print(f"Target person rows: {result.target_rows:,} [{result.status}]")
        print(
            "Concept_id 0: "
            f"gender={result.gender_concept_zero:,}, "
            f"race={result.race_concept_zero:,}, "
            f"ethnicity={result.ethnicity_concept_zero:,}"
        )
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "observation-period":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = transform_observation_period(config)
        except Exception as exc:
            print(f"ERROR: observation-period stage failed: {exc}")
            return 2
        write_run_manifest(config, status="observation_period_ready")
        print(f"Enrollment source rows: {result.source_rows:,}")
        print(f"Eligible rows: {result.eligible_rows:,}")
        print(f"Excluded rows: {result.excluded_rows:,}")
        print(f"  missing PATID: {result.excluded_missing_patid:,}")
        print(f"  missing start date: {result.excluded_missing_start_date:,}")
        print(f"  missing end date: {result.excluded_missing_end_date:,}")
        print(f"  invalid interval: {result.excluded_invalid_interval:,}")
        print(f"  unlinked person: {result.excluded_unlinked_person:,}")
        print(f"Unknown ENR_BASIS rows: {result.unknown_basis_rows:,}")
        print(
            "Overlapping/adjacent enrollment pairs: "
            f"{result.overlapping_or_adjacent_pairs:,}"
        )
        print(
            f"Target observation_period rows: {result.target_rows:,} "
            f"[{result.status}]"
        )
        print(f"period_type_concept_id 0: {result.period_type_concept_zero:,}")
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "visit-occurrence":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = transform_visit_occurrence(config)
        except Exception as exc:
            print(f"ERROR: visit-occurrence stage failed: {exc}")
            return 2
        write_run_manifest(config, status="visit_occurrence_ready")
        print(f"Encounter source rows: {result.source_rows:,}")
        print(f"Eligible rows: {result.eligible_rows:,}")
        print(f"Excluded rows: {result.excluded_rows:,}")
        print(f"  missing ENCOUNTERID: {result.excluded_missing_encounterid:,}")
        print(f"  missing PATID: {result.excluded_missing_patid:,}")
        print(f"  unlinked person: {result.excluded_unlinked_person:,}")
        print(f"  missing ADMIT_DATE: {result.excluded_missing_admit_date:,}")
        print(f"  missing DISCHARGE_DATE: {result.excluded_missing_discharge_date:,}")
        print(f"  invalid interval: {result.excluded_invalid_interval:,}")
        print(f"Unknown ENC_TYPE rows: {result.unknown_enc_type_rows:,}")
        print(
            f"Target visit_occurrence rows: {result.target_rows:,} "
            f"[{result.status}]"
        )
        print(f"visit_concept_id 0: {result.visit_concept_zero:,}")
        print(f"visit_source_concept_id 0: {result.visit_source_concept_zero:,}")
        print(f"Visit lineage crosswalk rows: {result.crosswalk_rows:,}")
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "condition-occurrence":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = transform_condition_occurrence(config)
        except Exception as exc:
            print(f"ERROR: condition-occurrence stage failed: {exc}")
            return 2
        write_run_manifest(config, status="condition_occurrence_ready")
        print(f"DIAGNOSIS source rows: {result.diagnosis_source_rows:,}")
        print(f"  eligible: {result.diagnosis_eligible_rows:,}")
        print(f"  excluded: {result.diagnosis_excluded_rows:,}")
        print(f"    missing DIAGNOSISID: {result.diagnosis_missing_id:,}")
        print(f"    missing PATID: {result.diagnosis_missing_patid:,}")
        print(f"    unlinked person: {result.diagnosis_unlinked_person:,}")
        print(f"    missing DX_DATE: {result.diagnosis_missing_dx_date:,}")
        print(f"CONDITION source rows: {result.condition_source_rows:,}")
        print(f"  eligible: {result.condition_eligible_rows:,}")
        print(f"  excluded: {result.condition_excluded_rows:,}")
        print(f"    missing CONDITIONID: {result.condition_missing_id:,}")
        print(f"    missing PATID: {result.condition_missing_patid:,}")
        print(f"    unlinked person: {result.condition_unlinked_person:,}")
        print(f"    missing onset/report date: {result.condition_missing_date:,}")
        print(f"    invalid interval: {result.condition_invalid_interval:,}")
        print(f"  REPORT_DATE fallback rows: {result.condition_report_date_fallback:,}")
        print(f"Target condition_occurrence rows: {result.target_rows:,} [{result.status}]")
        print(f"  from DIAGNOSIS: {result.diagnosis_target_rows:,}")
        print(f"  from CONDITION: {result.condition_target_rows:,}")
        print(
            "condition_concept_id 0: "
            f"DIAGNOSIS={result.diagnosis_concept_zero:,}, "
            f"CONDITION={result.condition_concept_zero:,}"
        )
        print(
            "condition_source_concept_id 0: "
            f"DIAGNOSIS={result.diagnosis_source_concept_zero:,}, "
            f"CONDITION={result.condition_source_concept_zero:,}"
        )
        print(
            "visit_occurrence linked: "
            f"DIAGNOSIS={result.diagnosis_visit_linked:,}, "
            f"CONDITION={result.condition_visit_linked:,}"
        )
        print(f"Condition lineage rows: {result.lineage_rows:,}")
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "condition-mapping-audit":
        if not config.sql_password:
            env_name = config.raw["sqlserver"].get("password_env", "OMOP_SQL_PASSWORD")
            print(f"ERROR: SQL Server password environment variable is not set: {env_name}")
            return 2
        try:
            result = audit_condition_mapping(config)
        except Exception as exc:
            print(f"ERROR: condition-mapping-audit stage failed: {exc}")
            return 2
        write_run_manifest(config, status="condition_mapping_audited")
        print(f"Condition rows audited: {result.audited_rows:,}")
        print(f"condition_concept_id 0: {result.zero_rows:,}")
        print(f"  DIAGNOSIS: {result.diagnosis_zero_rows:,}")
        print(f"  CONDITION: {result.condition_zero_rows:,}")
        print(f"Audit: {result.audit_path}")
        return 0

    if args.command == "preflight":
        decision_errors = validate_decisions(config.raw)
        pending = unresolved_decisions(config.raw)
        result = run_preflight(config)
        errors = list(result.errors) + decision_errors
        warnings = list(result.warnings)
        if pending:
            warnings.append(
                f"{len(pending)} ETL policy decision(s) remain unresolved; run "
                f"'pcornet-omop-etl configure --config {config.path}'."
            )

        ok = result.ok and not decision_errors
        if args.as_json:
            print(
                json.dumps(
                    {
                        "ok": ok,
                        "errors": errors,
                        "warnings": warnings,
                        "found_tables": result.found_tables,
                        "unresolved_decisions": [spec.key for spec in pending],
                    },
                    indent=2,
                )
            )
        else:
            print("Preflight: " + ("PASS" if ok else "FAIL"))
            for warning in warnings:
                print(f"WARNING: {warning}")
            for error in errors:
                print(f"ERROR: {error}")
        return 0 if ok else 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
