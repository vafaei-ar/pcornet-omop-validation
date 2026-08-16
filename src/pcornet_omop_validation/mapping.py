"""Known PCORnet-to-OMOP domain relationships from the supplied ETL package.

These are hypotheses about intended ETL flow, not assumptions of record-level
one-to-one equivalence. Validation code should test the observed data.
"""

DOMAIN_MAP = {
    "PCORnet_DEMOGRAPHIC": ["person"],
    "PCORnet_ENROLLMENT": ["observation_period"],
    "PCORnet_ENCOUNTER": ["visit_occurrence", "location", "care_site"],
    "PCORnet_DIAGNOSIS": ["condition_occurrence"],
    "PCORnet_CONDITION": ["condition_occurrence"],
    "PCORnet_DISPENSING": ["drug_exposure"],
    "PCORnet_MED_ADMIN": ["drug_exposure"],
    "PCORnet_LAB_RESULT_CM": ["measurement"],
    "PCORnet_OBS_GEN": ["observation"],
    "PCORnet_DEATH": ["death"],
    "PCORnet_DEATH_CAUSE": ["death"],
    "PCORnet_IMMUNIZATION": ["drug_exposure", "procedure_occurrence"],
    "PCORnet_LDS_ADDRESS_HISTORY": ["location"],
}

PATIENT_ID_CANDIDATES = {
    "pcornet": ["PATID", "patid"],
    "omop": ["person_id", "PERSON_ID"],
}

SOURCE_PATIENT_ID_CANDIDATES = ["person_source_value", "PERSON_SOURCE_VALUE"]

KEY_HINTS = (
    "id",
    "patid",
    "encounterid",
    "providerid",
)

DATE_HINTS = ("date", "datetime", "time", "dt")
