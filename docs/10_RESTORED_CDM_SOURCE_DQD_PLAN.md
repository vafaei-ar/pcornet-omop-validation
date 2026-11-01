# 10 - Prepare CDM_SOURCE for isolated restored-copy DQD

_2026-11-01 | Draft pre-execution plan, not an executed database migration._

**Institutional test update (2026-11-01):** Full repository regression suite
passed **277 tests in 2.60s** after adding the offline CDM_SOURCE
manifest validator and its synthetic tests. This validates preparatory
code only. No factual manifest, protected backup copy, isolated restore,
CDM_SOURCE insert or DQD result has been produced.


## Status and decision

The publication database `OMOP_VALIDATED_A` is frozen. Its
`dbo.cdm_source` contains **zero** rows; the 11 expected OMOP CDM v5.4
columns exist. Source vocabulary metadata is present. DQD has
not run, because it requires populated `CDM_SOURCE`. Do **not**
insert a record into the frozen publication database.

The institution locally verified one checksum-enabled COPY_ONLY backup
(23.03 GiB compressed), readable by the SQL Server service.
`RESTORE FILELISTONLY` reports two logical files (108.32 GiB data,
55.01 GiB log, total 163.33 GiB) and 810.53 GiB local free space.
This is **not** an off-machine backup or a successful isolated restore.

**Scientific scope:** DQD on a restored copy with factual `CDM_SOURCE`
provenance is an explicitly labeled post-freeze target-only OMOP quality
analysis. It does not amend or certify the Stage A-E
PCORnet-to-OMOP manuscript results. Restored-copy DQD findings cannot
be ascribed retroactively to the pristine frozen database without
explicitly disclosing the metadata amendment and unchanged clinical tables.

## Metadata mapping: authoritative sources

The official OMOP CDM v5.4 `CDM_SOURCE` definition is
<https://ohdsi.github.io/CommonDataModel/cdm54.html>.
The OHDSI SQL example is
<https://ohdsi.github.io/CommonDataModel/sqlScripts.html>.
DQD confirms the metadata requirement:
<https://github.com/OHDSI/DataQualityDashboard/blob/main/README.md>.

| CDM_SOURCE field | Evidence, value or unresolved input |
| --- | --- |
| `cdm_source_name` | Approved descriptive name for the isolated restored copy; must not misidentify the unmodified original |
| `cdm_source_abbreviation` | Approved abbreviation, maximum 25 characters |
| `cdm_holder` | Actual institution that controls this dataset, **requires data-owner confirmation** |
| `source_description` | Source PCORnet dataset, restricted coverage, known omitted/empty OMOP domains and isolated-DQD purpose |
| `source_documentation_reference` | Verifiable source data dictionary/specification identifier, not a guess |
| `cdm_etl_reference` | Frozen ETL commit `887e6f4d60a6b185e58b3c9fe8887472b49777e3` and code provenance |
| `source_release_date` | **Unknown until verified** against actual source extraction/snapshot records. Do not use today's date |
| `cdm_release_date` | **Unknown until verified** against original frozen ETL completion/provisioning record. Do not use restore date |
| `cdm_version` | `v5.4` official vocabulary label for the implemented OMOP v5.4.2 schema |
| `cdm_version_concept_id` | Candidate `756265` (OMOP CDM Version 5.4.0, official v5.4 example); **verify this concept ID exists and is the CDM version concept in the restored CONCEPT table** |
| `vocabulary_version` | **Obtain exactly** from restored `dbo.vocabulary` where `vocabulary_id = 'None'`; previously confirmed a nonempty local value but not exported |

CDM_SOURCE is metadata, **not a conversion of individual PCORnet
clinical rows**. Dates describe the original data release and ETL
completion, even when the metadata row is added after a restore.

## Stage 1: private metadata manifest (no SQL, no writes)

Use `study_definitions/restored_cdm_source_manifest.EXAMPLE.json` as
the schema reference. Create the actual file only under ignored,
institution-controlled
`results/postfreeze_standard_qc/cdm_source_manifest.json`.
Do not commit any populated instance-specific metadata. Protect the
directory `0700`, file `0600`. Populate the 11 fields **only after**
checking origin records; identify the documentary provenance of
`cdm_holder`, both dates, `cdm_version_concept_id`, and
`vocabulary_version` under `evidence`.

```bash
cd ~/works/repos/pcornet-omop-validation
git pull --ff-only
pytest -q

# Preview the template; edit the PRIVATE copy with a local secured editor.
umask 077
mkdir -p results/postfreeze_standard_qc
chmod 700 results/postfreeze_standard_qc
# Do not overwrite an existing private manifest:
test ! -e results/postfreeze_standard_qc/cdm_source_manifest.json &&
  cp -n study_definitions/restored_cdm_source_manifest.EXAMPLE.json \
    results/postfreeze_standard_qc/cdm_source_manifest.json
chmod 600 results/postfreeze_standard_qc/cdm_source_manifest.json

python scripts/validate_restored_cdm_source_manifest.py \
  --manifest results/postfreeze_standard_qc/cdm_source_manifest.json
```

The example contains intentionally blank required values and
**must fail validation until completed**. `PRELIMINARY PASS` indicates
syntactic completeness only; it does not certify that the source
dates, institutional holder or vocabulary value are authentic.
The validator makes no SQL connection and writes no file.

## Stage 2: isolate and independently restore

Requires an institutionally approved encrypted **off-machine**
destination, a named separate SQL Server environment, recovery
authorization and protected credentials, with no backup media in the
repository, chat, or public/cloud artifacts. Follow institutional DBA
backup-transfer procedures, compare original and destination
cryptographic checksums privately, check logical file sizes and
operational headroom, perform an isolated restore with data/log
files at approved paths into a new nonpublication database. Verify
the three frozen clinical counts independently:

- `person = 27,089`
- `visit_occurrence = 1,510,957`
- `condition_occurrence = 7,315,572`

Also check `CDM_SOURCE = 0` in the **unmodified restored snapshot**
and validate additional table snapshots/fingerprints if required by
the institutional review. Preserve the source snapshot and original
backup. Neither the existing R runner nor its launcher is currently
clone-ready: both deliberately pin the frozen target/database identity.
**Do not loosen or bypass those guards.**

## Stage 3: controlled metadata insert into restored database only

A separate restore-specific, minimal-write implementation must be
reviewed and tested **after** the actual isolated target identity,
schema, SQL privileges, approved backup restore record and metadata
have been confirmed. It must:

1. Reject database identity `OMOP_VALIDATED_A` and any ambiguous,
   preexisting metadata row; require a new unambiguously named
   restored-copy target and explicit operator authorization.
2. In a transaction, verify population anchors, original source
   metadata count zero, expected schema, unique `vocabulary_id='None'`
   version, and the actual `CDM` version concept.
3. Insert only one documentary `dbo.cdm_source` row, using bound
   parameters; verify one row afterwards and unchanged frozen-copy
   clinical table counts, then commit. Otherwise roll back.
4. Record this as a restored-copy **metadata amendment**, not
   original ETL output. Do not write clinical tables.

**No insertion script or DQD clone execution has been authorized or
executed yet.** This plan does not bypass the restore and institutional
data-governance prerequisites.

## Stage 4: DQD on clone, review before reporting

Adapt the DQD launcher and R runner **in a distinct clone-only code
path** with explicit new target identity, a dedicated restricted
read-only login, separate QC results/scratch output database,
version-pinned DQD and preserved clinical counts. The existing
frozen-target runner must not be relabeled or run against the new clone.
Keep default DQD thresholds. DQD `writeToTable=FALSE` may write
protected JSON logs on the local filesystem.

Review all check results, failures and applicability and obtain
institutional disclosure approval. Report both the restored-copy
metadata amendment and the frozen original's empty `CDM_SOURCE`.
DQD is *descriptive target quality assessment*, not proof of ETL
semantic fidelity or full-domain completeness.

## Manuscript timing

The completed Stage A-E findings and approved post-freeze mechanism
sensitivities are sufficient for a conservative co-author draft.
DQD can proceed as a separate extension. Do not delay identification
and revision of the actual authoritative manuscript DOCX waiting for
this isolated QC work, unless new DQD results are explicitly required
for a claim.
