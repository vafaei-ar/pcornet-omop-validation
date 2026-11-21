#!/usr/bin/env python3
"""Post-freeze death-aware 90-day stroke outcome sensitivity (aggregate only).

Reuses the independently verified D0 cohorts and frozen Stage D event rules from
scripts/audit_stage_d_90d_observability_mortality.py. A recorded death between
index discharge and day 89 shortens the *required observed interval* to death.
Events after that date do not count; same-calendar-day death/events count because
hour-level ordering is unavailable. Cases with death before index discharge are
excluded from this sensitivity and counted as a data-quality diagnostic.

This is NOT a replacement for prespecified Stage D. Do not upload row-level data.
Only writes a local aggregate JSON in the gitignored results/ directory.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Any

from audit_stage_d_90d_observability_mortality import (
    FROZEN_ETL_SHA, STUDY_PATH, D0_PATH, DEFAULT_PARENT,
    _sha256, _git, _schema, _sql_list, _load_anchors,
    _cohort_sql, _flags_sql, _query_groups, _validate_matches,
)

DEFAULT_DIAGNOSTIC = Path(
    'results/publication_analysis/postfreeze_observability_mortality/'
    'postfreeze_observability_mortality_90d.json'
)
DEFAULT_OUTPUT = Path(
    'results/publication_analysis/postfreeze_deathaware_90d/'
    'postfreeze_deathaware_90d_sensitivity.json'
)


def deathaware_cutoff(discharge: date, death: date | None) -> date | None:
    """Pure policy reference: None = invalid death before discharge."""
    if death is not None and death < discharge:
        return None
    end = discharge + timedelta(days=90)
    return min(death, end) if death is not None else end


def valid_event(event: date | None, discharge: date, cutoff: date | None) -> bool:
    """First qualifying event occurs in (discharge, min(death, day90)]."""
    return event is not None and cutoff is not None and discharge < event <= cutoff


def load_diagnostic(path: Path, parent: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f'Completed post-freeze observability diagnostic not found: {path}. '
            'Use --audit-summary with the existing original diagnostic JSON; do not rerun Stage D.'
        )
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('status') != 'postfreeze_observability_mortality_90d_diagnostic_complete':
        raise RuntimeError('Post-freeze diagnostic is not marked complete')
    if data.get('cohort_and_outcome_anchors_match') is not True:
        raise RuntimeError('Post-freeze diagnostic failed cohort/outcome anchors')
    for key, expected in (
        ('frozen_etl_sha', FROZEN_ETL_SHA),
        ('parent_stage_d_sha256', _sha256(parent)),
        ('study_definition_sha256', _sha256(STUDY_PATH)),
        ('d0_definition_sha256', _sha256(D0_PATH)),
    ):
        if data.get(key) != expected:
            raise RuntimeError(f'Post-freeze diagnostic anchor mismatch: {key}')
    return data


def deathaware_sql(src: str, tgt: str) -> str:
    """One SQL Server #temp row per D0 patient; read-only against source and OMOP."""
    return f"""
    SELECT a.patid, a.admission_era, a.age_years,
      a.retained_in_omop, a.same_index,
      a.source_covered90, a.target_covered90,
      a.source_event90, a.target_event90,
      CASE WHEN a.source_death_date < a.discharge_date THEN 1 ELSE 0 END src_invalid_death,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN a.target_death_date < o.omop_discharge_date THEN 1 ELSE 0 END tgt_invalid_death,
      CASE WHEN a.source_death_date >= a.discharge_date
                 AND a.source_death_date < DATEADD(day,90,a.discharge_date)
           THEN 1 ELSE 0 END src_early_death,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN a.target_death_date >= o.omop_discharge_date
                 AND a.target_death_date < DATEADD(day,90,o.omop_discharge_date)
           THEN 1 ELSE 0 END tgt_early_death,
      CASE WHEN a.source_death_date < a.discharge_date THEN 0
           WHEN EXISTS (
             SELECT 1 FROM [{src}].[PCORnet_ENROLLMENT] en
             WHERE CONVERT(nvarchar(255),en.PATID)=a.patid
               AND CAST(en.ENR_START_DATE AS date)<=a.discharge_date
               AND CAST(en.ENR_END_DATE AS date)>=cut.src_end
           ) THEN 1 ELSE 0 END src_eligible,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN a.target_death_date < o.omop_discharge_date THEN 0
           WHEN EXISTS (
             SELECT 1 FROM [{tgt}].[observation_period] op
             WHERE op.person_id=o.person_id
               AND op.observation_period_start_date<=o.omop_discharge_date
               AND op.observation_period_end_date>=cut.tgt_end
           ) THEN 1 ELSE 0 END tgt_eligible,
      CASE WHEN sfirst.first_date IS NOT NULL AND sfirst.first_date<=cut.src_end
                 AND (a.source_death_date IS NULL OR a.source_death_date>=a.discharge_date)
           THEN 1 ELSE 0 END src_event,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN tfirst.first_date IS NOT NULL AND tfirst.first_date<=cut.tgt_end
                 AND (a.target_death_date IS NULL OR a.target_death_date>=o.omop_discharge_date)
           THEN 1 ELSE 0 END tgt_event,
      CASE WHEN sfirst.first_date=a.source_death_date
                 AND a.source_death_date>=a.discharge_date
                 AND a.source_death_date<=DATEADD(day,90,a.discharge_date)
           THEN 1 ELSE 0 END src_first_event_on_death_day,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN tfirst.first_date=a.target_death_date
                 AND a.target_death_date>=o.omop_discharge_date
                 AND a.target_death_date<=DATEADD(day,90,o.omop_discharge_date)
           THEN 1 ELSE 0 END tgt_first_event_on_death_day,
      CASE WHEN sfirst.first_date>a.source_death_date
                 AND a.source_death_date>=a.discharge_date
                 AND a.source_death_date<=DATEADD(day,90,a.discharge_date)
           THEN 1 ELSE 0 END src_first_event_after_death,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN tfirst.first_date>a.target_death_date
                 AND a.target_death_date>=o.omop_discharge_date
                 AND a.target_death_date<=DATEADD(day,90,o.omop_discharge_date)
           THEN 1 ELSE 0 END tgt_first_event_after_death,
      CASE WHEN o.patid IS NULL THEN NULL
           WHEN (a.source_death_date IS NULL AND a.target_death_date IS NOT NULL)
             OR (a.source_death_date IS NOT NULL AND a.target_death_date IS NULL)
             OR (a.source_death_date<>a.target_death_date)
           THEN 1 ELSE 0 END death_record_disagreement
    INTO #deathaware90
    FROM #audit90 a
    JOIN #src_d0 s ON s.patid=a.patid
    LEFT JOIN #omop_d0 o ON o.patid=a.patid
    CROSS APPLY (
      SELECT
        CASE WHEN a.source_death_date>=a.discharge_date
                   AND a.source_death_date<DATEADD(day,90,a.discharge_date)
             THEN a.source_death_date ELSE DATEADD(day,90,a.discharge_date) END AS src_end,
        CASE WHEN o.patid IS NULL THEN NULL
             WHEN a.target_death_date>=o.omop_discharge_date
                   AND a.target_death_date<DATEADD(day,90,o.omop_discharge_date)
             THEN a.target_death_date ELSE DATEADD(day,90,o.omop_discharge_date) END AS tgt_end
    ) cut
    OUTER APPLY (
      SELECT MIN(CAST(e.ADMIT_DATE AS date)) first_date
      FROM [{src}].[PCORnet_ENCOUNTER] e
      WHERE CONVERT(nvarchar(255),e.PATID)=a.patid
        AND CAST(e.ADMIT_DATE AS date)>a.discharge_date
        AND CAST(e.ADMIT_DATE AS date)<=DATEADD(day,90,a.discharge_date)
        AND UPPER(LTRIM(RTRIM(CONVERT(nvarchar(50),e.ENC_TYPE)))) IN ('ED','EI','IP')
    ) sfirst
    OUTER APPLY (
      SELECT MIN(CAST(v.visit_start_date AS date)) first_date
      FROM [{tgt}].[visit_occurrence] v
      WHERE v.person_id=o.person_id
        AND CAST(v.visit_start_date AS date)>o.omop_discharge_date
        AND CAST(v.visit_start_date AS date)<=DATEADD(day,90,o.omop_discharge_date)
        AND v.visit_concept_id IN (9203,262,9201)
    ) tfirst;
    CREATE UNIQUE CLUSTERED INDEX IX_deathaware90_patid ON #deathaware90(patid);
    """


def aggregate_sql(side: str, where: str) -> str:
    if side not in ('src','tgt'):
        raise ValueError('side must be src or tgt')
    prefix = 'source' if side == 'src' else 'target'
    return f"""
    SELECT COUNT_BIG(*) n,
      SUM(CASE WHEN {prefix}_covered90=1 THEN 1 ELSE 0 END) original_eligible,
      SUM(CASE WHEN {prefix}_covered90=1 AND {prefix}_event90=1 THEN 1 ELSE 0 END) original_events,
      SUM(CASE WHEN {side}_eligible=1 THEN 1 ELSE 0 END) eligible,
      SUM(CASE WHEN {side}_eligible=1 AND {side}_event=1 THEN 1 ELSE 0 END) events,
      SUM(CASE WHEN {prefix}_covered90=0 AND {side}_eligible=1 THEN 1 ELSE 0 END) added,
      SUM(CASE WHEN {prefix}_covered90=1 AND {side}_eligible=0 THEN 1 ELSE 0 END) removed,
      SUM(CASE WHEN {side}_early_death=1 THEN 1 ELSE 0 END) early_deaths,
      SUM(CASE WHEN {side}_early_death=1 AND {side}_eligible=1 THEN 1 ELSE 0 END) eligible_early_deaths,
      SUM(CASE WHEN {side}_early_death=1 AND {side}_eligible=0 THEN 1 ELSE 0 END) excluded_early_deaths,
      SUM(CASE WHEN {side}_early_death=1 AND {prefix}_covered90=0 AND {side}_eligible=1
               THEN 1 ELSE 0 END) added_early_deaths,
      SUM(CASE WHEN {side}_invalid_death=1 THEN 1 ELSE 0 END) invalid_pre_discharge_deaths,
      SUM(CASE WHEN {side}_first_event_on_death_day=1 THEN 1 ELSE 0 END) first_events_on_death_day,
      SUM(CASE WHEN {side}_first_event_after_death=1 THEN 1 ELSE 0 END) first_events_after_death,
      SUM(CASE WHEN {side}_eligible=1 AND {prefix}_event90=1 AND {side}_event=0
               THEN 1 ELSE 0 END) changed_positive_to_negative,
      AVG(CASE WHEN {side}_eligible=1 THEN CAST(age_years AS float) ELSE NULL END) eligible_mean_age
    FROM #deathaware90 WHERE {where}
    """


def fixed_sql() -> str:
    return """
    SELECT COUNT_BIG(*) eligible,
      SUM(CASE WHEN src_event=1 THEN 1 ELSE 0 END) source_events,
      SUM(CASE WHEN tgt_event=1 THEN 1 ELSE 0 END) omop_events,
      SUM(CASE WHEN src_event=1 AND tgt_event=1 THEN 1 ELSE 0 END) both_positive,
      SUM(CASE WHEN src_event=1 AND tgt_event=0 THEN 1 ELSE 0 END) source_only_positive,
      SUM(CASE WHEN src_event=0 AND tgt_event=1 THEN 1 ELSE 0 END) omop_only_positive,
      SUM(CASE WHEN src_event=0 AND tgt_event=0 THEN 1 ELSE 0 END) both_negative,
      SUM(CASE WHEN src_early_death=1 OR tgt_early_death=1 THEN 1 ELSE 0 END) early_deaths,
      SUM(CASE WHEN source_covered90=0 OR target_covered90=0 THEN 1 ELSE 0 END) newly_eligible
    FROM #deathaware90
    WHERE same_index=1 AND src_eligible=1 AND tgt_eligible=1
    """


def original_fixed_sql() -> str:
    """Reproduce all frozen fixed-index 90-day label components, not only its N."""
    return """
    SELECT COUNT_BIG(*) eligible,
      SUM(CASE WHEN source_event90=1 THEN 1 ELSE 0 END) source_events,
      SUM(CASE WHEN target_event90=1 THEN 1 ELSE 0 END) omop_events,
      SUM(CASE WHEN source_event90=1 AND target_event90=1 THEN 1 ELSE 0 END) both_positive,
      SUM(CASE WHEN source_event90=1 AND target_event90=0 THEN 1 ELSE 0 END) source_only_positive,
      SUM(CASE WHEN source_event90=0 AND target_event90=1 THEN 1 ELSE 0 END) omop_only_positive,
      SUM(CASE WHEN source_event90=0 AND target_event90=0 THEN 1 ELSE 0 END) both_negative
    FROM #deathaware90
    WHERE same_index=1 AND source_covered90=1 AND target_covered90=1
    """


def validate_original_fixed(actual: dict[str, Any], original: dict[str, Any]) -> None:
    locked=original['results']['primary_fixed_index_90d']['label_counts']
    for key in ('eligible','source_events','omop_events','both_positive',
                'source_only_positive','omop_only_positive','both_negative'):
        if int(actual[key])!=int(locked[key]):
            raise RuntimeError(f'Frozen fixed-index 90-day {key} mismatch')

def diagnostic_sql() -> str:
    return """
    SELECT COUNT_BIG(*) lineage_retained,
      SUM(CASE WHEN death_record_disagreement=1 THEN 1 ELSE 0 END) discordant_death_records,
      SUM(CASE WHEN src_eligible<>tgt_eligible THEN 1 ELSE 0 END) discordant_eligibility,
      SUM(CASE WHEN src_event<>tgt_event THEN 1 ELSE 0 END) discordant_event_flag,
      SUM(CASE WHEN src_invalid_death=1 OR tgt_invalid_death=1 THEN 1 ELSE 0 END) invalid_deaths
    FROM #deathaware90 WHERE retained_in_omop=1
    """


def clean_numeric(row: Any) -> dict[str, int | float | None]:
    out = {}
    for k,v in dict(row).items():
        if v is None:
            out[k]=None
        elif k == 'eligible_mean_age':
            out[k]=round(float(v),2)
        else:
            out[k]=int(v)
    return out


def _risk(events: int, denominator: int) -> float | None:
    return events/denominator if denominator else None


def compare(source: dict[str, Any], omop: dict[str, Any],
            source_event_key: str = 'events', omop_event_key: str = 'events',
            denom_key: str = 'eligible') -> dict[str, Any]:
    sr = _risk(source[source_event_key], source[denom_key])
    tr = _risk(omop[omop_event_key], omop[denom_key])
    return {
        'source_risk': sr,
        'omop_risk': tr,
        'absolute_difference_percentage_points': None if sr is None or tr is None else (tr-sr)*100,
        'omop_over_source_risk_ratio': None if sr in (None,0) or tr is None else tr/sr,
    }


def validate_aggregate(row: dict[str, Any]) -> None:
    n=row['n']; orig=row['original_eligible']; eligible=row['eligible']
    if not (0<=orig<=n and 0<=eligible<=n):
        raise RuntimeError('Invalid original or sensitivity denominator')
    if orig+row['added']-row['removed'] != eligible:
        raise RuntimeError('Eligibility roll-forward does not reconcile')
    if row['added'] > row['added_early_deaths'] or row['added'] > row['early_deaths']:
        raise RuntimeError('Death-aware rule added someone without a recorded early death')
    if row['removed'] > row['invalid_pre_discharge_deaths']:
        raise RuntimeError('Sensitivity removed patients for reasons beyond invalid death timing')
    if not (0<=row['events']<=eligible and 0<=row['original_events']<=orig):
        raise RuntimeError('Outcome count outside denominator')
    if row['eligible_early_deaths']+row['excluded_early_deaths']!=row['early_deaths']:
        raise RuntimeError('Early-death eligibility count does not reconcile')
    if row['changed_positive_to_negative'] > row['first_events_after_death']+row['invalid_pre_discharge_deaths']:
        raise RuntimeError('Unexplained positive labels removed')


def validate_against_diagnostic(locked_rows: list[dict[str, Any]], diagnostic: dict[str, Any]) -> None:
    old = {(r['group_type'],r['group_label']):r for r in diagnostic['aggregate_rows']}
    new = {(r['group_type'],r['group_label']):r for r in locked_rows}
    for key in [('source_d0_all','source_d0_all'),('omop_lineage_retained','omop_lineage_retained')]:
        if key not in old or key not in new:
            raise RuntimeError(f'Missing diagnostic group: {key}')
        for col in ('n','source_covered90','source_events90_covered','source_deaths_before_day90'):
            if int(old[key][col])!=int(new[key][col]):
                raise RuntimeError(f'Post-freeze diagnostic recheck failed: {key}, {col}')
        if key[0]=='omop_lineage_retained':
            for col in ('target_covered90','target_events90_covered','target_deaths_before_day90'):
                if int(old[key][col])!=int(new[key][col]):
                    raise RuntimeError(f'Post-freeze diagnostic recheck failed: {key}, {col}')


def run(config_path: str, original_stage_d: Path, diagnostic_path: Path, output: Path) -> dict[str, Any]:
    from pcornet_omop_validation.etl.config import load_etl_config
    from pcornet_omop_validation.etl.database import make_engine
    from pcornet_omop_validation.study.stroke_codes import ICD9_STROKE_CODES, ICD10_STROKE_CODES

    if output.resolve() in (original_stage_d.resolve(), diagnostic_path.resolve()):
        raise ValueError('Sensitivity output cannot overwrite the frozen analysis or the diagnostic')
    original = _load_anchors(original_stage_d)
    diagnostic = load_diagnostic(diagnostic_path, original_stage_d)
    cfg = load_etl_config(config_path)
    study = json.loads(STUDY_PATH.read_text(encoding='utf-8'))
    if study.get('frozen_etl_sha') != FROZEN_ETL_SHA:
        raise RuntimeError('Stage D definition is not anchored to the frozen ETL')
    src = _schema(cfg.raw['sqlserver'].get('source_schema','dbo'))
    tgt = _schema(cfg.raw['sqlserver'].get('target_schema','dbo'))

    print('Reconstructing original frozen D0 and Stage D flags...',flush=True)
    engine = make_engine(cfg)
    try:
        with engine.connect() as con:
            con.exec_driver_sql('SET LOCK_TIMEOUT 30000')
            for statement in _cohort_sql(src,tgt,_sql_list(set(ICD9_STROKE_CODES) | set(ICD10_STROKE_CODES))):
                con.exec_driver_sql(statement)
            con.exec_driver_sql(_flags_sql(src,tgt))
            locked_rows = _query_groups(con)
            _validate_matches(locked_rows,original)
            validate_against_diagnostic(locked_rows,diagnostic)
            print('Original Stage D and post-freeze audit anchors verified.',flush=True)
            print('Applying death-aware observation and event cutoffs...',flush=True)
            con.exec_driver_sql(deathaware_sql(src,tgt))
            source = clean_numeric(con.exec_driver_sql(aggregate_sql('src','1=1')).mappings().one())
            retained_source = clean_numeric(con.exec_driver_sql(aggregate_sql('src','retained_in_omop=1')).mappings().one())
            source_only = clean_numeric(con.exec_driver_sql(aggregate_sql('src','retained_in_omop=0')).mappings().one())
            omop = clean_numeric(con.exec_driver_sql(aggregate_sql('tgt','retained_in_omop=1')).mappings().one())
            fixed_original = clean_numeric(con.exec_driver_sql(original_fixed_sql()).mappings().one())
            validate_original_fixed(fixed_original, original)
            fixed = clean_numeric(con.exec_driver_sql(fixed_sql()).mappings().one())
            quality = clean_numeric(con.exec_driver_sql(diagnostic_sql()).mappings().one())
    finally:
        engine.dispose()

    for name,group in (('source',source),('retained_source',retained_source),('source_only',source_only),('omop',omop)):
        try:
            validate_aggregate(group)
        except RuntimeError as exc:
            raise RuntimeError(f'{name} {exc}') from exc
    if source['n'] != retained_source['n']+source_only['n']:
        raise RuntimeError('Source D0 subgroups do not sum to D0 population')
    if source['eligible'] != retained_source['eligible']+source_only['eligible']:
        raise RuntimeError('Source D0 subgroup eligible counts fail reconciliation')
    if omop['n'] != retained_source['n']:
        raise RuntimeError('OMOP lineage retention count mismatch')
    if fixed['eligible'] > min(source['eligible'],omop['eligible']):
        raise RuntimeError('Fixed eligible denominator exceeds either population')
    if sum(fixed[k] for k in ('both_positive','source_only_positive','omop_only_positive','both_negative')) != fixed['eligible']:
        raise RuntimeError('Fixed joint outcome labels do not reconcile')

    end_to_end = compare(source,omop)
    fixed_risk = compare(fixed,fixed,source_event_key='source_events',omop_event_key='omop_events')
    payload = {
        'status':'postfreeze_deathaware_90d_sensitivity_complete',
        'run_utc':datetime.now(timezone.utc).isoformat(),
        'analysis_repo_sha':_git('rev-parse','HEAD'),
        'analysis_repo_clean':_git('status','--porcelain')=='',
        'frozen_etl_sha':FROZEN_ETL_SHA,
        'original_stage_d_sha256':_sha256(original_stage_d),
        'parent_mortality_diagnostic_sha256':_sha256(diagnostic_path),
        'd0_definition_sha256':_sha256(D0_PATH),
        'stage_d_definition_sha256':_sha256(STUDY_PATH),
        'original_stage_d_anchors_matched':True,
        'postfreeze_diagnostic_anchors_matched':True,
        'method':{
            'role':'post-freeze descriptive sensitivity, not a new prespecified primary estimand',
            'index':'locked source-selected D0 and source-selected D0-to-OMOP lineage',
            'observation':'require one source enrollment or OMOP observation period to cover discharge through min(90-day end, recorded death date); if death missing or at/after day90 require full day90',
            'outcome':'binary ED/EI/IP event start on days 1-90 and on/before recorded death when early death occurs',
            'death_date_ties':'an acute-care event on the recorded date of death counts; ordering within a calendar day is unknown and is reported separately',
            'invalid_death_timing':'exclude cases with recorded death before discharge from sensitivity; quantify separately',
            'missing_death':'absence of recorded death does not prove survival; such cases retain full day90 coverage requirement',
            'full_coverage_semantics':'same single-row continuous coverage semantics as frozen Stage D',
            'interpretation':'death-aware acute-care event proportion, treating recorded death before an event as competing termination, among records with coverage to death or day90',
        },
        'original_90d':original['results']['secondary_end_to_end_90d'],
        'original_fixed_90d_verified':fixed_original,
        'source_d0':source,
        'source_lineage_retained':retained_source,
        'source_only':source_only,
        'omop_lineage_retained':omop,
        'fixed_index':fixed,
        'quality':quality,
        'end_to_end_deathaware_comparison':end_to_end,
        'fixed_index_deathaware_comparison':fixed_risk,
        'guardrails':[
            'No prespecified Stage D eligibility, event definitions, indices, or equivalence margins were changed.',
            'No individual identifiers or patient-level records were written to disk.',
            'Aggregate cells may be small or reconstructible. Institutional disclosure approval required before sharing.',
            'Do not treat equivalence to a chosen margin in this post-freeze analysis as a confirmatory result.',
            'Recorded death and coverage association is not a causal analysis.',
        ],
    }
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(payload,indent=2,sort_keys=True),encoding='utf-8')
    print('status: postfreeze_deathaware_90d_sensitivity_complete')
    print('anchors: Stage D primary and preceding mortality diagnostic verified')
    print('source/OMOP death-aware denominators:',source['eligible'],omop['eligible'])
    print('source/OMOP death-aware events:',source['events'],omop['events'])
    print('private_aggregate_output:',output)
    print('DO NOT SHARE SMALL OR RECONSTRUCTIBLE CELLS WITHOUT INSTITUTIONAL APPROVAL')
    return payload


def self_test() -> None:
    sql=deathaware_sql('dbo','dbo').upper()
    assert 'INTO #DEATHAWARE90' in sql
    assert 'DATEADD(DAY,90' in sql
    assert 'PCORNET_ENROLLMENT' in sql and 'OBSERVATION_PERIOD' in sql
    assert 'PCORNET_ENCOUNTER' in sql and 'VISIT_OCCURRENCE' in sql
    assert 'CREATE UNIQUE CLUSTERED INDEX' in sql
    for forbidden in ('DROP TABLE [','DELETE FROM [','INSERT INTO [','UPDATE [','ALTER TABLE ['):
        assert forbidden not in sql
    assert deathaware_cutoff(date(2020,1,1),None)==date(2020,3,31)
    assert deathaware_cutoff(date(2020,1,1),date(2020,1,15))==date(2020,1,15)
    assert deathaware_cutoff(date(2020,1,1),date(2019,12,31)) is None
    print('self-test passed (offline; SQL Server was not queried)')


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',help='Existing institutional SQL Server config (prefer the validated OMOP_VALIDATED_A instance)')
    p.add_argument('--stage-d-summary',type=Path,default=DEFAULT_PARENT,
                   help='Archived completed Stage D JSON')
    p.add_argument('--audit-summary',type=Path,default=DEFAULT_DIAGNOSTIC,
                   help='Completed 90-day observability and mortality diagnostic JSON')
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT,
                   help='Local disclosure-controlled aggregate result JSON')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    if a.self_test:
        self_test()
        return
    if not a.config:
        p.error('--config is required unless --self-test is specified')
    run(a.config,a.stage_d_summary,a.audit_summary,a.output)


if __name__=='__main__':
    main()