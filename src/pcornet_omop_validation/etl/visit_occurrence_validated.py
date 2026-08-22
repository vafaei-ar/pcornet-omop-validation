from __future__ import annotations

import json

from sqlalchemy import text

from . import visit_occurrence as _legacy


# Keep only encounter categories whose target concepts have already been verified
# as current standard Visit-domain concepts in the loaded Athena vocabulary.
# Historical mappings that are absent or now belong to another domain are not
# replaced by guesswork. They are represented as concept_id 0 while ENC_TYPE is
# preserved in visit_source_value for auditability.
VALIDATED_VISIT_CONCEPT_MAP = {
    "AV": 38004207,
    "ED": 9203,
    "EI": 262,
    "IP": 9201,
    "IS": 0,
    "OS": 581385,
    "IC": 0,
    "OA": 0,
}

# PCORnet ENC_TYPE is a CDM category, not necessarily a vocabulary concept in the
# current Athena release. Preserve the source value and avoid inserting obsolete
# or nonexistent source concept IDs.
VALIDATED_VISIT_SOURCE_CONCEPT_MAP = {
    "AV": 0,
    "ED": 0,
    "EI": 0,
    "IP": 0,
    "IS": 0,
    "OS": 0,
    "IC": 0,
    "OA": 0,
    "NI": 0,
    "UN": 0,
    "OT": 0,
}


def _validate_nonzero_visit_concepts(connection, schema: str) -> None:
    ids = sorted({value for value in VALIDATED_VISIT_CONCEPT_MAP.values() if value > 0})
    if not ids:
        return
    values = ",".join(str(value) for value in ids)
    rows = connection.execute(
        text(
            f"""
            SELECT concept_id, concept_name, domain_id, standard_concept
            FROM [{schema}].[concept]
            WHERE concept_id IN ({values})
            """
        )
    ).fetchall()
    by_id = {int(row[0]): row for row in rows}
    missing = [concept_id for concept_id in ids if concept_id not in by_id]
    invalid = [
        row for row in rows
        if not (row[2] == "Visit" and row[3] == "S")
    ]
    if missing or invalid:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(str(value) for value in missing))
        if invalid:
            details.append(
                "not_standard_visit="
                + "; ".join(f"{row[0]}:{row[2]}:{row[3]}" for row in invalid)
            )
        raise RuntimeError(
            "Validated visit concept mapping failed vocabulary validation: "
            + " | ".join(details)
        )


def _safe_datetime_sql(date_column: str, time_column: str) -> str:
    """Combine a source date with PCORnet numeric seconds-since-midnight.

    In this PCORnet extract, ADMIT_TIME and DISCHARGE_TIME are floating-point
    seconds since midnight, with observed values from 0 through 86340.
    Values outside the valid [0, 86400) interval fall back to the source date
    at midnight rather than inventing a time.
    """
    seconds = f"TRY_CONVERT(float, {time_column})"
    return f"""
    CASE
      WHEN {date_column} IS NULL THEN NULL
      WHEN {seconds} IS NULL
        OR {seconds} < 0
        OR {seconds} >= 86400
        THEN CAST(CAST({date_column} AS date) AS datetime2(7))
      ELSE DATEADD(
        MILLISECOND,
        CAST(ROUND({seconds} * 1000.0, 0) AS bigint),
        CAST(CAST({date_column} AS date) AS datetime2(7))
      )
    END
    """.strip()



VisitOccurrenceTransformResult = _legacy.VisitOccurrenceTransformResult


def transform_visit_occurrence(config):
    # Patch the historical implementation at runtime so its reconciliation and
    # exclusion logic are retained while vocabulary-invalid mappings are removed.
    _legacy.VISIT_CONCEPT_MAP = dict(VALIDATED_VISIT_CONCEPT_MAP)
    _legacy.VISIT_SOURCE_CONCEPT_MAP = dict(VALIDATED_VISIT_SOURCE_CONCEPT_MAP)
    _legacy._validate_visit_concepts = _validate_nonzero_visit_concepts
    _legacy._datetime_sql = _safe_datetime_sql

    result = _legacy.transform_visit_occurrence(config)

    # Correct the audit narrative so it reflects the validated mapping policy.
    try:
        payload = json.loads(result.audit_path.read_text(encoding="utf-8"))
        strategy = payload.setdefault("mapping_strategy", {})
        strategy["visit_concept"] = (
            "Only runtime-validated standard Visit concepts are assigned; "
            "historical mappings absent from the current vocabulary or in another domain use concept_id 0"
        )
        strategy["visit_source_concept"] = (
            "0 for PCORnet ENC_TYPE categories; raw ENC_TYPE is preserved in visit_source_value"
        )
        strategy["encounter_time_handling"] = (
            "Time values are converted to text before TRY_CONVERT(time); values that cannot be parsed "
            "fall back to the source date at midnight rather than causing an ETL failure or inventing a time"
        )
        strategy["historical_mapping_validation_failure"] = {
            "44814710": "absent from loaded vocabulary",
            "44814711": "absent from loaded vocabulary",
            "4127751": "current domain is Observation, not Visit",
        }
        result.audit_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    except Exception:
        # The clinical transformation result should not be discarded solely because
        # post-hoc audit annotation failed. The original audit file still exists.
        pass

    return result
