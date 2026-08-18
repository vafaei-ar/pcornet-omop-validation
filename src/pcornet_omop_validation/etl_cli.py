from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pcornet_omop_validation.etl import (
    apply_omop_schema,
    load_etl_config,
    run_preflight,
    write_run_manifest,
)
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
        "acquire", help="Download pinned public OHDSI dependencies and record checksums"
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
        assets = acquire_public_dependencies(config)
        if not assets:
            print("Automatic public dependency download is disabled.")
            return 0
        for asset in assets:
            print(f"Acquired {asset.name} {asset.version}")
            print(f"  archive: {asset.archive}")
            print(f"  sha256:  {asset.sha256}")
        print(f"Dependency manifest: {config.audit_dir / 'dependencies.json'}")
        return 0

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
