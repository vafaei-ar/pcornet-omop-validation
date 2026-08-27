# PCORnet to OMOP validation

This repository develops a reproducible PCORnet-to-OMOP ETL and validation framework for evaluating whether transformation from the PCORnet Common Data Model to the OMOP Common Data Model preserves clinically and analytically relevant information.

The project does **not** assume that tables or row counts should be identical across CDMs. Validation proceeds from ETL/data understanding to semantic concordance, phenotype reproducibility, and analytical equivalence.

## Publication status

The audited ETL completed its final clean publication freeze at commit `887e6f4d60a6b185e58b3c9fe8887472b49777e3`. Downstream scientific analyses proceed on `publication/analysis` while treating that ETL commit as fixed.

See:

- `docs/project_history_and_decisions.md` for the durable project history, decisions, Stage A findings, Mermaid roadmap, and publication plan;
- `docs/current_status_and_publication_plan.md` for current project status and next steps;
- `docs/publication_etl_freeze_record.md` for the final ETL freeze acceptance record;
- `docs/publication_analysis_workflow.md` for the concordance, phenotype, and analytical-equivalence workflow;
- `docs/final_freeze_runbook.md` for the operational clean-build procedure used to establish the freeze;
- `docs/etl_redesign.md` for ETL architecture and design decisions.

## Local layout

```text
/usr/local/datasets/OMOP/
├── OMOP_parquet/                 # prior OMOP build retained for comparison
├── PCORnet_parquet/              # source PCORnet parquet tables
└── OMOP_validated_parquet/       # output of the new audited ETL
```

## Installation

```bash
cd pcornet-omop-validation
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[etl]'
```

For development/tests:

```bash
python -m pip install -e '.[etl,dev]'
pytest
```

## Audited PCORnet-to-OMOP ETL

The ETL targets OMOP CDM v5.4.2 on SQL Server first. It keeps the prior OMOP database untouched and uses a separate validated target database.

Create a local config:

```bash
cp config/etl.example.yaml config/etl.yaml
```

Set the SQL Server password without storing it in Git:

```bash
export OMOP_SQL_PASSWORD='your-password'
```

Inspect the planned stages and unresolved scientific decisions:

```bash
pcornet-omop-etl plan --config config/etl.yaml
```

Download pinned public OHDSI dependencies and record checksums:

```bash
pcornet-omop-etl acquire --config config/etl.yaml
```

Athena vocabulary files are user-provided because vocabulary selection, authentication, and licensing may require user action. Put the downloaded Athena files in the directory configured under `vocabulary.directory`.

Resolve explicit ETL/scientific decisions interactively:

```bash
pcornet-omop-etl configure --config config/etl.yaml
```

Run preflight checks:

```bash
pcornet-omop-etl preflight --config config/etl.yaml
```

Write the current provenance/configuration manifest:

```bash
pcornet-omop-etl manifest --config config/etl.yaml
```

Create the isolated target database if needed and apply the pinned official OHDSI OMOP v5.4 SQL Server DDL:

```bash
pcornet-omop-etl schema --config config/etl.yaml
```

The schema command is non-destructive. Destructive reset behavior is isolated behind the separately guarded clean-reset command and exact database/schema confirmations.

## Run the initial profile

```bash
pcornet-omop-profile \
  --pcornet /usr/local/datasets/OMOP/PCORnet_parquet \
  --omop /usr/local/datasets/OMOP/OMOP_parquet \
  --output results
```

Or:

```bash
cp config/example.yaml config/local.yaml
pcornet-omop-profile --config config/local.yaml
```

## Run the transformation trace

```bash
python scripts/02_transform_trace.py \
  --pcornet /usr/local/datasets/OMOP/PCORnet_parquet \
  --omop /usr/local/datasets/OMOP/OMOP_parquet \
  --output results/transform_trace
```

The `results/` directory is ignored by Git. Zip aggregate results for review when needed:

```bash
zip -r pcornet_omop_profile_results.zip results/
```

## Validation and publication stages

1. Audited ETL and structural fidelity — **frozen**.
2. Stage A structural/semantic concordance — **in progress; core aggregate and manuscript-oriented summaries generated**.
3. Stage B patient-level semantic concordance.
4. Stage C phenotype reproducibility.
5. Stage D analytical equivalence and sensitivity analyses.

## Data governance

Do not commit source parquet files, Athena vocabulary packages, database credentials, or returned result bundles. Aggregate outputs can still contain sensitive information. Review outputs before sharing outside the approved research environment. The repository is public, so only code and non-sensitive documentation should be committed.
