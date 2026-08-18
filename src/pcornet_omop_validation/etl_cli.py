from __future__ import annotations

import argparse
import json
import sys

from pcornet_omop_validation.etl import load_etl_config, run_preflight


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_etl_config(args.config)

    if args.command == "plan":
        print(f"OMOP CDM version: {config.raw['etl']['cdm_version']}")
        print(f"Backend: {config.raw['etl']['backend']}")
        print("Stages:")
        for index, stage in enumerate(config.stages, start=1):
            print(f"  {index:02d}. {stage}")
        return 0

    if args.command == "preflight":
        result = run_preflight(config)
        if args.as_json:
            print(
                json.dumps(
                    {
                        "ok": result.ok,
                        "errors": result.errors,
                        "warnings": result.warnings,
                        "found_tables": result.found_tables,
                    },
                    indent=2,
                )
            )
        else:
            print("Preflight: " + ("PASS" if result.ok else "FAIL"))
            for warning in result.warnings:
                print(f"WARNING: {warning}")
            for error in result.errors:
                print(f"ERROR: {error}")
        return 0 if result.ok else 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
