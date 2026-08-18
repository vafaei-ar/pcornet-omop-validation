from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DecisionOption:
    value: str
    label: str
    description: str


@dataclass(frozen=True)
class DecisionSpec:
    key: str
    title: str
    rationale: str
    options: tuple[DecisionOption, ...]
    recommended: str | None = None


DECISIONS: tuple[DecisionSpec, ...] = (
    DecisionSpec(
        key="missing_required_date",
        title="Missing source date for an OMOP record",
        rationale=(
            "Some OMOP fact tables require a date. Replacing missing dates with an artificial "
            "date such as 1900-01-01 changes the clinical meaning and can bias temporal analyses."
        ),
        options=(
            DecisionOption(
                "exclude",
                "Exclude the record",
                "Do not emit an OMOP fact when its required source date is absent; report the count and reason.",
            ),
            DecisionOption(
                "derive",
                "Derive from an explicitly related date",
                "Use a predefined domain-specific fallback date only when its clinical meaning is defensible and auditable.",
            ),
            DecisionOption(
                "sentinel",
                "Use a sentinel date",
                "Emit an artificial date. This is supported only for reproducing legacy behavior and is not recommended.",
            ),
        ),
        recommended="exclude",
    ),
    DecisionSpec(
        key="unmapped_standard_concept",
        title="Source code has no standard OMOP mapping",
        rationale=(
            "OMOP commonly represents an unmapped standard concept with concept_id 0 while preserving source values."
        ),
        options=(
            DecisionOption(
                "concept_zero",
                "Use concept_id 0 and preserve source",
                "Retain the record, preserve source value/source concept where possible, and quantify the unmapped rate.",
            ),
            DecisionOption(
                "exclude",
                "Exclude unmapped records",
                "Drop records that cannot be mapped to a standard concept and report exclusions.",
            ),
        ),
        recommended="concept_zero",
    ),
    DecisionSpec(
        key="heterogeneous_observation_domain",
        title="Target domain for OBS_CLIN and OBS_GEN concepts",
        rationale=(
            "Clinical/general observations may map to standard concepts whose OMOP domain is not Observation."
        ),
        options=(
            DecisionOption(
                "route_by_standard_domain",
                "Route by standard concept domain",
                "Send mapped records to the OMOP domain indicated by the standard concept when supported.",
            ),
            DecisionOption(
                "observation_only",
                "Keep all records in observation",
                "Reproduce the simpler legacy behavior even when the standard concept domain differs.",
            ),
        ),
        recommended="route_by_standard_domain",
    ),
    DecisionSpec(
        key="condition_sources",
        title="Representation of DIAGNOSIS and CONDITION",
        rationale=(
            "PCORnet DIAGNOSIS and CONDITION are distinct source domains and may overlap clinically. "
            "Silently omitting either source is not acceptable."
        ),
        options=(
            DecisionOption(
                "include_both",
                "Include both with source lineage",
                "Transform both sources and preserve provenance so overlap can be quantified and audited.",
            ),
            DecisionOption(
                "diagnosis_only",
                "DIAGNOSIS only",
                "Exclude CONDITION from condition_occurrence and document the exclusion.",
            ),
            DecisionOption(
                "condition_only",
                "CONDITION only",
                "Exclude DIAGNOSIS from condition_occurrence and document the exclusion.",
            ),
        ),
        recommended="include_both",
    ),
)


def unresolved_decisions(raw_config: dict[str, Any]) -> list[DecisionSpec]:
    policies = raw_config.get("policies", {}) or {}
    return [spec for spec in DECISIONS if policies.get(spec.key) in (None, "", "ask")]


def validate_decisions(raw_config: dict[str, Any]) -> list[str]:
    policies = raw_config.get("policies", {}) or {}
    errors: list[str] = []
    for spec in DECISIONS:
        value = policies.get(spec.key)
        if value in (None, "", "ask"):
            continue
        allowed = {option.value for option in spec.options}
        if value not in allowed:
            errors.append(
                f"Invalid policies.{spec.key}={value!r}; choose one of: {', '.join(sorted(allowed))}"
            )
    return errors


def prompt_for_decisions(raw_config: dict[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for spec in unresolved_decisions(raw_config):
        print(f"\n{spec.title}")
        print(spec.rationale)
        for idx, option in enumerate(spec.options, start=1):
            marker = " [recommended]" if option.value == spec.recommended else ""
            print(f"  {idx}. {option.label}{marker}\n     {option.description}")
        while True:
            answer = input("Choose an option number: ").strip()
            try:
                option = spec.options[int(answer) - 1]
            except (ValueError, IndexError):
                print("Please enter one of the listed option numbers.")
                continue
            selected[spec.key] = option.value
            break
    return selected


def write_decision_log(
    path: str | Path,
    *,
    config_path: str | Path,
    decisions: dict[str, str],
    source: str,
) -> None:
    payload = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": str(config_path),
        "source": source,
        "decisions": decisions,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)
