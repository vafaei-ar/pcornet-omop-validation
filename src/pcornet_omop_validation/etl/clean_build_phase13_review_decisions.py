from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from .config import EtlConfig, load_etl_config
from .freeze_decision_review import review_freeze_decisions
from .global_reconciliation import reconcile_validated_etl
from .semantic_freeze_audit import audit_semantic_freeze


_TYPE_FLAG_RE = re.compile(r"^(?P<table>[a-z_]+) has (?P<count>[0-9,]+) rows with type concept 0$")
_DRUG_ROUTE_FLAG_RE = re.compile(
    r"^drug_exposure has (?P<count>[0-9,]+) rows with nonblank route source but route concept 0$"
)
_DEATH_TYPE_FLAG_RE = re.compile(
    r"^death has (?P<count>[0-9,]+) rows with death_type_concept_id 0 by explicit provenance policy$"
)


def _classify_review_flags(
    semantic: dict[str, object], review: dict[str, object]
) -> tuple[list[dict[str, object]], list[str]]:
    explained: list[dict[str, object]] = []
    unexplained: list[str] = []
    type_zero = {str(k): int(v) for k, v in dict(review["type_zero_rows"]).items()}
    route_zero = int(review["drug_nonblank_route_zero_rows"])

    for flag_obj in list(semantic.get("review_flags", [])):
        flag = str(flag_obj)
        m = _TYPE_FLAG_RE.match(flag)
        if m:
            table = m.group("table")
            count = int(m.group("count").replace(",", ""))
            if type_zero.get(table) == count:
                explained.append(
                    {
                        "flag": flag,
                        "basis": "prespecified KEEP_ZERO type-concept policy",
                        "count_reconciled": True,
                    }
                )
                continue

        m = _DRUG_ROUTE_FLAG_RE.match(flag)
        if m:
            count = int(m.group("count").replace(",", ""))
            if count == route_zero:
                explained.append(
                    {
                        "flag": flag,
                        "basis": "unique exact active Standard Route mapping rule; unresolved standardized route values remain 0",
                        "count_reconciled": True,
                    }
                )
                continue

        m = _DEATH_TYPE_FLAG_RE.match(flag)
        if m:
            count = int(m.group("count").replace(",", ""))
            if type_zero.get("death") == count:
                explained.append(
                    {
                        "flag": flag,
                        "basis": "explicit Death provenance policy: no exact OMOP Death Type inferred from PCORnet DEATH_SOURCE",
                        "count_reconciled": True,
                    }
                )
                continue

        unexplained.append(flag)

    return explained, unexplained


def run_clean_build_phase13_review_decisions(config: EtlConfig) -> dict[str, object]:
    # All three operations are read-only with respect to OMOP clinical tables.
    global_result = reconcile_validated_etl(config)
    if global_result.get("status") != "matched":
        raise RuntimeError(
            f"Global reconciliation is not matched: {global_result.get('status')}"
        )

    semantic = audit_semantic_freeze(config)
    hard_blockers = [str(x) for x in semantic.get("hard_blockers", [])]
    if hard_blockers:
        raise RuntimeError(f"Semantic hard blockers remain: {hard_blockers}")

    review = review_freeze_decisions(config)
    if review.get("status") != "reviewed":
        raise RuntimeError(
            f"Semantic decision review did not complete: {review.get('status')}"
        )

    explained, unexplained = _classify_review_flags(semantic, review)
    semantic_flags = [str(x) for x in semantic.get("review_flags", [])]
    if len(explained) + len(unexplained) != len(semantic_flags):
        raise RuntimeError("Review-flag accounting is internally inconsistent")
    if unexplained:
        raise RuntimeError(
            "Unexplained semantic review flags remain; freeze candidate is not approved: "
            f"{unexplained}"
        )

    status = "freeze_candidate_reviewed"
    payload = {
        "stage": "clean_build_phase13_review_decisions",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(config.raw["sqlserver"].get("database")),
        "target_schema": str(config.raw["sqlserver"].get("target_schema", "dbo")),
        "status": status,
        "global_reconciliation_status": global_result.get("status"),
        "semantic_freeze_status": semantic.get("status"),
        "semantic_hard_blockers": hard_blockers,
        "semantic_review_flags": semantic_flags,
        "explained_review_flags": explained,
        "unexplained_review_flags": unexplained,
        "type_zero_rows": review.get("type_zero_rows"),
        "drug_nonblank_route_zero_rows": review.get("drug_nonblank_route_zero_rows"),
        "type_decision": review.get("type_decision"),
        "route_decision_rule": review.get("route_decision_rule"),
        "note": (
            "This status means all current semantic review flags are explicitly explained by "
            "prespecified source/provenance policies and reconcile to the rebuilt database. "
            "It does not replace any later repository/documentation release or comparator analysis."
        ),
    }

    audit_path = config.audit_dir / "clean_build_phase13_review_decisions.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    payload["audit_path"] = str(audit_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only semantic review decision phase for the clean rebuild."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    result = run_clean_build_phase13_review_decisions(load_etl_config(args.config))
    print("status:", result["status"])
    print("database:", result["database"])
    print("target_schema:", result["target_schema"])
    print("global_reconciliation_status:", result["global_reconciliation_status"])
    print("semantic_freeze_status:", result["semantic_freeze_status"])
    print("semantic_hard_blockers:", result["semantic_hard_blockers"])
    print("semantic_review_flag_count:", len(result["semantic_review_flags"]))
    print("explained_review_flag_count:", len(result["explained_review_flags"]))
    print("unexplained_review_flags:", result["unexplained_review_flags"])
    print("type_zero_rows:", result["type_zero_rows"])
    print("drug_nonblank_route_zero_rows:", result["drug_nonblank_route_zero_rows"])
    print("Audit:", result["audit_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
