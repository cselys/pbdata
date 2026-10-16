"""Read-only report of committed shadow-session sampling coverage."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path


def committed_rows(directory, name, offsets):
    remaining=offsets[name]
    with (directory/name).open('rb') as stream:
        while remaining:
            line=stream.readline(remaining)
            if not line or not line.endswith(b'\n'):
                raise ValueError('Incomplete committed JSONL record: '+name)
            remaining-=len(line)
            yield json.loads(line)


def coverage(directory, checkpoint):
    directory=Path(directory)
    symbols=checkpoint['config']['symbols']; universe=set(symbols)
    per_symbol={s:dict(observations=0,observed_sweeps=0,successful_quotes=0,sampling_gaps=0,max_sampling_gap_seconds=None) for s in symbols}
    previous={};partial=[];sweeps=0;segment=1;key=None;seen=set();duplicates=0
    first=None;last=None;statuses=Counter();sessions=Counter();counts=Counter();max_gap=checkpoint['config']['detector']['max_gap']
    gap_ends=Counter();stall_batches=0
    timing={};outliers=[]
    def finish():
        if key is not None and universe-seen:
            partial.append(dict(segment=key[0],sweep=key[1],missing_symbols=sorted(universe-seen)))
    for record in committed_rows(directory,'observations.jsonl',checkpoint['offsets']):
        sweep=record.get('sweep',record['shadow_transaction'])
        if key is not None and sweep<key[1]: segment+=1
        new_key=(segment,sweep)
        if new_key!=key:
            finish();key=new_key;seen=set();sweeps+=1
        for row in record['observations']:
            s=row['symbol']
            if s not in universe:raise ValueError('Unconfigured symbol in committed observations')
            duplicates+=s in seen
            per_symbol[s]['observed_sweeps']+=s not in seen
            seen.add(s)
            per_symbol[s]['observations']+=1
            success=row.get('category')=='success' and isinstance(row.get('http_status'),int) and 200<=row['http_status']<300
            per_symbol[s]['successful_quotes']+=success
            counts['observations']+=1;counts['unsuccessful_quote_observations']+=not success
            statuses[str(row.get('http_status'))]+=1
            sessions[str(row.get('market_session'))]+=1
            stamp=row.get('response_received_at_utc')
            if stamp:
                current=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                first=min(first,current) if first else current;last=max(last,current) if last else current
                if s in previous:
                    gap=(current-previous[s]).total_seconds()
                    old=per_symbol[s]['max_sampling_gap_seconds']
                    per_symbol[s]['max_sampling_gap_seconds']=max(gap,old) if old is not None else gap
                    per_symbol[s]['sampling_gaps']+=gap>max_gap
                previous[s]=current
        for batch in record.get('batches',[]):
            op=batch.get('operation_duration_monotonic_ms');latency=batch.get('http_latency_ms')
            if isinstance(op,(int,float)) and not isinstance(op,bool) and isinstance(latency,(int,float)) \
                    and not isinstance(latency,bool) and op-latency>5000:
                stall_batches+=1
            for field in ('idle_wait_monotonic_ms','postprocess_duration_monotonic_ms',
                          'commit_duration_monotonic_ms','wall_monotonic_drift_ms'):
                value=batch.get(field)
                if isinstance(value,(int,float)) and not isinstance(value,bool):
                    stat=timing.setdefault(field,dict(count=0,total=0.0,minimum=value,maximum=value))
                    stat['count']+=1;stat['total']+=value
                    stat['minimum']=min(stat['minimum'],value);stat['maximum']=max(stat['maximum'],value)
                    if abs(value)>5000 and field!='wall_monotonic_drift_ms':
                        outliers.append(dict(transaction=record.get('shadow_transaction'),
                            field=field,value=value))
    finish()
    interval_status=Counter()
    for r in committed_rows(directory,'intervals.jsonl',checkpoint['offsets']):
        interval_status[r['status']]+=1
        gap=r.get('actual_observation_interval_seconds')
        if isinstance(gap,(int,float)) and not isinstance(gap,bool) and gap>max_gap:
            gap_ends[r['status']]+=1
    quality={s:n for s,n in interval_status.items() if s not in ('first_valid_observation','baseline_warmup','below_threshold','spike_candidate','zero_baseline')}
    sampling_gaps=sum(v['sampling_gaps'] for v in per_symbol.values())
    never=[s for s,v in per_symbol.items() if not v['observations']]
    no_success=[s for s,v in per_symbol.items() if not v['successful_quotes']]
    reasons=[]
    for condition,label in [(not sweeps,'no_observations'),(partial,'partial_sweeps'),(duplicates,'duplicate_symbol_observations_in_sweep'),
        (never,'unobserved_symbols'),(no_success,'symbols_without_successful_quotes'),
        (counts['unsuccessful_quote_observations'],'unsuccessful_quotes'),(sampling_gaps,'sampling_gaps'),(quality,'quality_exclusions')]:
        if condition:reasons.append(label)
    for s,v in per_symbol.items():v['missing_observed_sweep_samples']=sweeps-v['observed_sweeps']
    return dict(status='incomplete' if reasons else 'complete_within_observed_sweeps',reasons=reasons,
        scope='Configured universe within recorded sweeps only; not full trading-day or intended-duration coverage. Entire unrecorded sweeps cannot be reconstructed.',
        configured_symbols=len(symbols),observed_segments=segment if sweeps else 0,observed_sweeps=sweeps,
        committed_transactions=checkpoint['transaction'],observations=counts['observations'],
        expected_observations_within_recorded_sweeps=sweeps*len(symbols),
        missing_observed_sweep_samples=sum(len(r['missing_symbols']) for r in partial),
        partial_sweeps=partial,duplicate_symbol_observations_in_sweep=duplicates,
        never_observed_symbols=never,symbols_without_successful_quotes=no_success,
        unsuccessful_quote_observations=counts['unsuccessful_quote_observations'],
        sampling_gap_threshold_seconds=max_gap,sampling_gaps=sampling_gaps,
        first_receive_utc=first.isoformat() if first else None,last_receive_utc=last.isoformat() if last else None,
        market_sessions_observed=dict(sessions),full_market_session_coverage='not_evaluated',
        quality_exclusions=quality,interval_status_counts=dict(interval_status),http_status_observation_counts=dict(statuses),per_symbol=per_symbol,
        sampling_gaps_by_end_status=dict(gap_ends),scheduler_stall_batches=stall_batches,
        timing={k:dict(count=v['count'],minimum=v['minimum'],maximum=v['maximum'],
            mean=v['total']/v['count']) for k,v in timing.items()},
        timing_outliers=outliers)


def render(report):
    rows=['# Shadow session-end coverage report','',f"Coverage: **{report['status']}**",'',report['scope'],'',
        f"Committed transactions: {report['committed_transactions']}; observations: {report['observations']}; recorded sweeps: {report['observed_sweeps']}.",
        f"Missing symbol samples within recorded sweeps: {report['missing_observed_sweep_samples']}; gaps above {report['sampling_gap_threshold_seconds']} seconds: {report['sampling_gaps']}.",
        f"Sampling-gap ends by interval status: `{json.dumps(report.get('sampling_gaps_by_end_status',{}))}`.",
        f"Scheduler stall batches (operation duration minus HTTP latency above 5000 ms): {report.get('scheduler_stall_batches',0)}.",
        f"Receive-time range (UTC): {report['first_receive_utc']} to {report['last_receive_utc']}.",
        'Observed market-session labels: `'+json.dumps(report['market_sessions_observed'])+'`. Full scheduled market-session coverage is not evaluated.',
        'Reasons: '+(', '.join(report['reasons']) or 'none')+'.',
        'Quality exclusions: `'+json.dumps(report['quality_exclusions'])+'`.', '',
        'A completed process status does not establish complete market coverage. --duration is per invocation; no cumulative duration or full-day coverage is inferred.', '',
        '## Partial sweeps','']
    if report.get('timing'):
        rows += ['', '## Timing diagnostics','',
            f"Aggregates: `{json.dumps(report['timing'])}`.",
            f"Timing outliers (|value| above 5000 ms): `{json.dumps(report.get('timing_outliers',[]))}`."]
    rows += [f"- Segment {r['segment']}, sweep {r['sweep']}: missing {', '.join(r['missing_symbols'])}." for r in report['partial_sweeps']]
    if not report['partial_sweeps']:rows.append('None.')
    rows += ['', '## Per-symbol coverage','', '| Symbol | Observations | Successful quotes | Missing sweep samples | Sampling gaps |', '|---|---:|---:|---:|---:|']
    rows += [f"| {s} | {r['observations']} | {r['successful_quotes']} | {r['missing_observed_sweep_samples']} | {r['sampling_gaps']} |" for s,r in sorted(report['per_symbol'].items())]
    return '\n'.join(rows)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    source=args.session.resolve();out=args.output_dir.resolve()
    if out==source or source in out.parents:parser.error('Read-only reporting requires an output directory outside the session')
    cp=json.loads((source/'checkpoint.json').read_text(encoding='utf-8'))
    report=coverage(source,cp)
    out.mkdir(parents=True,exist_ok=True)
    (out/'session_end.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (out/'SESSION_END_REPORT.md').write_text(render(report),encoding='utf-8')
    print('Coverage:',report['status'],'Report:',out/'SESSION_END_REPORT.md')


if __name__=='__main__':main()
