# 11 - Authorized metadata-only CDM_SOURCE amendment for DQD

_2026-11-01: Study owner explicitly authorized adding CDM_SOURCE metadata to
the existing frozen publication database while preserving all clinical data.
This **supersedes the isolated-restore prerequisite** described in document
10 for this specific one-row metadata amendment. It does not supersede
institutional data governance or the separate backup-recovery requirement._

## What changed and why

- **Target:** existing `OMOP_VALIDATED_A.dbo.cdm_source`, with zero rows.
- **Change permitted:** insert **exactly one** verified CDM_SOURCE metadata
  record. No clinical table updates, ETL rerun, schema change, backup transfer,
  restored copy, new QC principal or SQL privilege grant.
- **Previously verified protections:** 23.03 GiB readable local COPY_ONLY
  backup; previous `RESTORE VERIFYONLY WITH CHECKSUM`; exact backup FILELISTONLY
  source identity; 163.33 GiB logical file allocation and 810.53 GiB local
  free space. Backup remains service-owned and untouched.
- **Original A-E baseline:** original ETL commit
  `887e6f4d60a6b185e58b3c9fe8887472b49777e3`. After the insert,
  explicitly label `CDM_SOURCE` as a **post-freeze metadata-only amendment**.
  Do not claim the pristine frozen database contained this row.
- **Downstream:** Existing DQD R runner uses read-only
  `ohdsi_postfreeze_qc` login, unmodified pinned DQD 2.9.1 and
  `writeToTable=FALSE`, with separate QC results database.

OHDSI sources: <https://ohdsi.github.io/CommonDataModel/cdm54.html>,
<https://ohdsi.github.io/CommonDataModel/sqlScripts.html> and
<https://ohdsi.github.io/DataQualityDashboard/>.

## Required factual metadata (no fabricated values)

The existing example manifest
`study_definitions/restored_cdm_source_manifest.EXAMPLE.json`
is **generic field structure** even though its historical filename
mentions restored copies. All required strings/dates are deliberately blank.

The study operator must verify and fill, privately:

1. `cdm_source_name`, abbreviation (<=25 chars), real `cdm_holder`
   and brief PCORnet source description, with the holder approved by the
   institutional data custodian.
2. `source_documentation_reference` and `cdm_etl_reference`. Include the
   exact frozen commit ID in the ETL reference.
3. **`source_release_date`** from original PCORnet
   source snapshot/extraction evidence, **not today's date**.
4. **`cdm_release_date`** from original completion
   of the frozen ETL, **not today's metadata insert date**.
5. `cdm_version="v5.4"` and `cdm_version_concept_id=756265`
   (official OMOP v5.4 example). The connected frozen CONCEPT table must
   independently contain this CDM/Metadata concept.
6. `vocabulary_version` from this target's exact
   `dbo.vocabulary` record `vocabulary_id='None'`. The insert preflight
   compares this value, privately, with the live database.
7. Non-empty documentary `evidence` references for holder, two dates,
   concept ID and vocabulary. Syntax validation is **not** fact checking.

Keep instance-specific data-holder details, release dates, infrastructure
paths and private evidence references **only in ignored local files**.
No populated institutional manifest belongs in the repo or chat.

## Operator commands: create local private manifest and test

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q

umask 077
mkdir -p results/postfreeze_standard_qc
chmod 700 results/postfreeze_standard_qc
test ! -e results/postfreeze_standard_qc/cdm_source_manifest.json &&
  cp study_definitions/restored_cdm_source_manifest.EXAMPLE.json \
     results/postfreeze_standard_qc/cdm_source_manifest.json
chmod 600 results/postfreeze_standard_qc/cdm_source_manifest.json

# Edit only the private file using the local editor, after factual source checks.
# Do not place values in your terminal transcript or ChatGPT.
python scripts/validate_restored_cdm_source_manifest.py \
  --manifest results/postfreeze_standard_qc/cdm_source_manifest.json
```

The deliberately blank example **will block** until a factual private
record has been completed. The offline validator merely checks completeness
and file permissions; it does not contact SQL.

## Read-only live preflight before any amendment

```bash
python scripts/amend_frozen_cdm_source.py
# PREVIEW ONLY, no SQL

python scripts/amend_frozen_cdm_source.py --preflight
# Prompts for the existing authorized ETL SQL login password if needed.
# SELECT-only SQL identity, frozen count, schema, vocabulary, concept checks.
```

Do not bypass a `BLOCKED` stage. Preflight never inserts a row.

## Transactional metadata-only insertion (after PRELIMINARY + live PASS)

The operator may **explicitly** run the one-row insert after reviewing
the factual manifest, confirming the successful read-only preflight, and
checking institutional governance permits this metadata change:

```bash
python scripts/amend_frozen_cdm_source.py --execute \
  --confirm INSERT_ONE_CDM_SOURCE_METADATA_ROW_IN_OMOP_VALIDATED_A
```

The script uses the existing verified SQL administrator credential,
prompts without echo if not present, verifies the target identity and
row-count anchors, refuses a nonempty `CDM_SOURCE` table, requires
exact v5.4 schema, compares the exact `vocabulary_version`, validates
CDM concept `756265`, checks insert permission, inserts with SQL
parameters in one database transaction, re-reads the row, rechecks
locked clinical counts, and commits **only on success**. Otherwise it
rolls back. This is intentionally **not** idempotent: a second run
blocks because the row already exists. No raw driver errors or protected
field values are printed. No backup, DDL, data copy, clinical table
modification or DQD run occurs during insertion.

The original metadata count was zero. After this specific amendment,
one row is the expected state. Record the date of amendment and the
operator/approval in a protected institutional audit location, separately
from `cdm_release_date` and without exposing patient data.

## Run DQD afterward with existing restricted QC principal

After `FROZEN CDM_SOURCE METADATA AMENDMENT: PASS`, the operator
can use the established launcher (it independently validates the
frozen clinical anchors and current source metadata count).

```bash
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool dqd

# ONLY if previous DQD output directory is empty and the approved
# restricted QC login/password is available through local secret handling:
python scripts/launch_postfreeze_omop_qc.py \
  --config config/etl_A.yaml --tool dqd --execute
```

**Do not delete or overwrite preexisting partial DQD outputs** to defeat
the create-only output gate. Preserve and review them locally first.
DQD JSON and logs can reveal sensitive small counts; retain them
in ignored, permission-restricted local output, and run aggregate
disclosure review before reporting or sharing any DQD findings.
A completed DQD run does not certify the ETL or imply full OMOP domain
coverage.

## Evidence gates and current status

- Latest completed institutional suite before the amendment code:
  **277 passed in 2.60s**. New guarded insertion code/tests have been
  committed but **not yet run on institutional machine**.
- Existing backup verified: **yes**.
- Clinical database unchanged by preparation: **yes**.
- Real verified metadata record: **not yet prepared**.
- `CDM_SOURCE` row inserted: **no**.
- DQD check results: **none**.

No further infrastructure should be made a prerequisite for this approved
metadata-only update. A separate protected recovery strategy remains a
worthwhile operational task but is distinct from DQD enablement.
