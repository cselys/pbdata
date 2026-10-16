"""Instrumented shadow scanner. Replay is offline; live mode must be explicitly selected."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from public_api_test.volume_spikes import VolumeEngine, detect, timestamp
from public_api_test.volume_quality import EventEngine, cluster

VERSION = 1
FILES = ('observations.jsonl', 'intervals.jsonl', 'events.jsonl', 'events.csv')
EVENT_FIELDS = ('transition_id','transaction','transition','at','processed_at_utc','reason',
    'event_id','symbol','decision_time','decision_available_at_utc','start','end','peak_rvol',
    'total_volume_delta','candidate_volume_delta','candidate_count','duration_seconds',
    'alert_eligible','cooldown_suppressed')


def utc():
    return datetime.now(timezone.utc)


def safe(value):
    """Additional report boundary: credentials/account fields are never persisted."""
    if isinstance(value,dict):
        return {k:('[REDACTED]' if any(t in re.sub('[^a-z]','',k.lower()) for t in
            ('token','secret','authorization','apikey','cookie','password','account')) else safe(v))
            for k,v in value.items()}
    if isinstance(value,list):
        return [safe(v) for v in value]
    if isinstance(value,str):
        value=re.sub(r'(?i)Bearer\s+[^\s"<>]+','Bearer [REDACTED]',value)
        return re.sub(r'(/marketdata/)[^/]+(/quotes)',r'\1[REDACTED]\2',value)
    return value


def encoded(value):
    return (json.dumps(safe(value),allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')


def atomic_json(path, value):
    temp=path.with_name(path.name+'.tmp')
    with temp.open('wb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)


def metric(summary, name, value):
    if value is None or not math.isfinite(value):
        return
    state=summary.setdefault(name,dict(count=0,total=0,minimum=value,maximum=value,mean=0))
    state['count']+=1; state['total']+=value
    state['minimum']=min(state['minimum'],value); state['maximum']=max(state['maximum'],value)
    state['mean']=state['total']/state['count']


class ShadowStore:
    """One writer; fsynced append logs + atomic checkpoint with committed offsets.

    A failed transaction poisons the current instance. Reopen with resume to roll
    back uncommitted tails. Output IDs and detector state commit together.
    """
    def __init__(self, directory, config, *, resume=False, source=None):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.lock=(self.directory/'writer.lock').open('a+b')
        self.poisoned=False
        try:
            if self.lock.seek(0,2)==0:
                self.lock.write(b'0');self.lock.flush()
            self.lock.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            checkpoint=self.directory/'checkpoint.json'
            if resume:
                state=json.loads(checkpoint.read_text(encoding='utf-8'))
                # Token-configuration keys are per-invocation credential choices: sessions
                # recorded before these keys existed resume when everything else matches.
                compatible=(state['config']==config or
                    state['config']=={k:v for k,v in config.items()
                        if k not in ('validity_in_minutes','credential_source')})
                if state['version']!=VERSION or not compatible or state['source']!=source:
                    raise ValueError('Resume requires the same schema, source, universe and configuration')
                for name,offset in state['offsets'].items():
                    path=self.directory/name
                    if path.stat().st_size < offset:
                        raise ValueError('Committed output is missing or truncated: '+name)
                    with path.open('r+b') as stream:
                        stream.truncate(offset)
                self.state=state
            else:
                if any(p.name!='writer.lock' for p in self.directory.iterdir()):
                    raise ValueError('Output directory must be empty; use --resume for an existing run')
                for name in FILES:
                    (self.directory/name).write_bytes(b'')
                stream=io.StringIO(newline='')
                csv.DictWriter(stream,fieldnames=EVENT_FIELDS).writeheader()
                (self.directory/'events.csv').write_bytes(stream.getvalue().encode())
                self.state=dict(version=VERSION,run_id=uuid4().hex,config=config,source=source,
                    transaction=0,volume=None,events=None,diagnostics={},metrics={},runtime={},
                    transition_counts={},offsets={name:(self.directory/name).stat().st_size for name in FILES})
                atomic_json(checkpoint,self.state)
            self.volume=VolumeEngine(**config['detector'],state=self.state['volume'])
            self.events=EventEngine(config['detector']['threshold'],config['event_gap'],config['cooldown'],state=self.state['events'])
            self.summary('ready')
        except BaseException:
            self.lock.close()
            raise

    def _append(self,name,payload):
        with (self.directory/name).open('ab') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
            return stream.tell()

    def process(self,record, *, processed_at=None, runtime=None, timing=None):
        if self.poisoned:
            raise RuntimeError('Failed transaction requires restart recovery')
        # Reject universe/schema drift before any state changes.
        symbols=[r['symbol'] for r in record['observations']]
        expected=record.get('expected_symbols',self.state['config']['symbols'])
        if len(symbols)!=len(set(symbols)) or not set(symbols)<=set(self.state['config']['symbols']) or not set(expected)<=set(self.state['config']['symbols']):
            raise ValueError('Unexpected or duplicate symbols in observation record')
        now=(processed_at or utc()).isoformat()
        try:
            t_commit0 = time.monotonic() if timing is not None else None
            seq=self.state['transaction']+1
            intervals=self.volume.process(record,expected_symbols=expected)
            changes=[]
            original={r['symbol']:r for r in record['observations']}
            for row in intervals:
                symbol=row['symbol']
                diagnostic=self.state['diagnostics'].setdefault(symbol,dict(zero_run=0,last_receive=None))
                raw_volume=original[symbol].get('volume')
                row['raw_cumulative_volume']=raw_volume
                try:
                    raw_number=float(raw_volume) if not isinstance(raw_volume,bool) else float('nan')
                except (TypeError,ValueError,OverflowError):
                    raw_number=float('nan')
                row['volume_observation_quality']=('missing' if raw_volume is None else
                    'invalid' if not math.isfinite(raw_number) else 'negative' if raw_number<0 else 'nonnegative')
                row['rejected_negative_volume_change']=(row['cumulative_volume']-row['previous_cumulative_volume']
                    if row['status']=='negative_volume_correction_or_reset' else None)
                prior_time=timestamp(diagnostic['last_receive'])
                received=timestamp(row['received_at_utc'])
                actual_gap=(received-prior_time).total_seconds() if received and prior_time else None
                metric(self.state['metrics'],'per_symbol_polling_interval_seconds',actual_gap)
                metric(diagnostic,'sampling_interval_seconds',actual_gap)
                row['actual_observation_interval_seconds']=actual_gap
                row['zero_then_burst_suspected']=(diagnostic['zero_run']>0 and row['status']=='spike_candidate')
                row['prior_zero_intervals']=diagnostic['zero_run']
                diagnostic['zero_run']=diagnostic['zero_run']+1 if row['volume_delta']==0 else 0
                diagnostic['last_receive']=row['received_at_utc']
                if row['zero_then_burst_suspected']:
                    self.state['transition_counts']['zero_then_burst_suspected']=self.state['transition_counts'].get('zero_then_burst_suspected',0)+1
                changes.extend(self.events.process(row))
            missing=set(expected)-set(symbols)
            for symbol in missing:
                changes.extend(self.events.close(symbol,now,'missing_observation'))
                self.state['diagnostics'].pop(symbol,None)
            for batch in record.get('batches',[]):
                key='http_status_'+str(batch.get('http_status'))
                counts=self.state['transition_counts']; counts[key]=counts.get(key,0)+1
                if batch.get('category') not in ('success','partial_result','empty_or_failed_quotes'):
                    counts['request_failure_batches']=counts.get('request_failure_batches',0)+1
                elif batch.get('category')!='success':
                    counts['partial_or_empty_batches']=counts.get('partial_or_empty_batches',0)+1
                category='request_category_'+str(batch.get('category'))
                counts[category]=counts.get(category,0)+1
                metric(self.state['metrics'],'http_latency_ms',batch.get('http_latency_ms'))
                metric(self.state['metrics'],'operation_duration_monotonic_ms',batch.get('operation_duration_monotonic_ms'))
            first_batch=not record.get('batches') or record['batches'][0].get('batch_index',1)==1
            for key in ('actual_polling_interval_seconds','scheduling_lateness_seconds'):
                if first_batch:
                    metric(self.state['metrics'],key,record.get(key))
            if record.get('final_batch_in_sweep',True):
                metric(self.state['metrics'],'batch_skew_seconds',record.get('batch_skew_seconds'))
            public_changes=[]
            for index,change in enumerate(changes):
                event=change['event']
                active=self.events.symbols[event['symbol']]['active']
                if change['transition']=='open':
                    event['decision_available_at_utc']=now
                    if active and active['event_id']==event['event_id']:
                        active['decision_available_at_utc']=now
                event=dict(event,event_id=self.state['run_id']+':'+event['event_id'])
                item=dict(change,event=event,transaction=seq,processed_at_utc=now,
                    transition_id=f"{self.state['run_id']}:{seq}:{index}")
                public_changes.append(item)
                counts=self.state['transition_counts'];kind=change['transition']
                counts[kind]=counts.get(kind,0)+1
                if kind=='open' and event['alert_eligible']:
                    counts['alert_opportunities']=counts.get('alert_opportunities',0)+1
            raw=dict(record,shadow_transaction=seq,processed_at_utc=now)
            payloads={'observations.jsonl':encoded(raw),
                'intervals.jsonl':b''.join(encoded(dict(row,transaction=seq)) for row in intervals),
                'events.jsonl':b''.join(encoded(c) for c in public_changes)}
            csv_stream=io.StringIO(newline='')
            writer=csv.DictWriter(csv_stream,fieldnames=EVENT_FIELDS,extrasaction='ignore')
            for c in public_changes:
                writer.writerow(safe(dict(c,**c['event'])))
            payloads['events.csv']=csv_stream.getvalue().encode('utf-8')
            t_app0 = time.monotonic() if timing is not None else None
            offsets={name:self._append(name,payloads[name]) for name in FILES}
            if timing is not None:
                timing['append_fsync_ms']=max(0.0,(time.monotonic()-t_app0)*1000)
            self.state.update(transaction=seq,volume=self.volume.state(),events=self.events.symbols,
                offsets=offsets,runtime=runtime if runtime is not None else self.state['runtime'])
            t_ckpt0 = time.monotonic() if timing is not None else None
            atomic_json(self.directory/'checkpoint.json',self.state)
            if timing is not None:
                timing['checkpoint_write_ms']=max(0.0,(time.monotonic()-t_ckpt0)*1000)
            t_sum0 = time.monotonic() if timing is not None else None
            self.summary('running')
            if timing is not None:
                timing['summary_write_ms']=max(0.0,(time.monotonic()-t_sum0)*1000)
                timing['other_commit_ms']=max(0.0,(time.monotonic()-t_commit0)*1000
                    -timing['append_fsync_ms']-timing['checkpoint_write_ms']-timing['summary_write_ms'])
            return intervals,public_changes
        except BaseException:
            self.poisoned=True
            raise

    def summary(self,status, error=None):
        state=self.state
        counts=state['volume']['counts'] if state['volume'] else {}
        value=dict(version=VERSION,run_id=state['run_id'],status=status,error=error,
            committed_transactions=state['transaction'],config=state['config'],source=state['source'],
            interval_counts=counts,transition_counts=state['transition_counts'],metrics=state['metrics'],
            runtime=state['runtime'],active_right_censored_events=[dict(s['active'],
                event_id=state['run_id']+':'+s['active']['event_id'],right_censored=True)
                for s in (state['events'] or {}).values() if s['active']],
            per_symbol=state['volume']['per_symbol'] if state['volume'] else {},
            sampling_intervals_by_symbol={s:d.get('sampling_interval_seconds') for s,d in state['diagnostics'].items()})
        if status not in ('ready','running'):
            from public_api_test.shadow_report import coverage,render
            value['market_coverage']=coverage(self.directory,state)
            atomic_json(self.directory/'session_end.json',value['market_coverage'])
            report_path=self.directory/'SESSION_END_REPORT.md'
            temp=report_path.with_suffix('.md.tmp')
            temp.write_text(render(value['market_coverage']),encoding='utf-8')
            os.replace(temp,report_path)
        atomic_json(self.directory/'summary.json',value)
        return value

    def close(self):
        self.lock.close()


def configuration(symbols, *, threshold=3,window=30,min_samples=10,max_age=60,max_gap=30,
                  event_gap=30,cooldown=120,interval=10,requests_per_minute=40,
                  validity_in_minutes=450,credential_source='fresh'):
    if not 1<=len(symbols)<=100 or len(set(symbols))!=len(symbols):
        raise ValueError('Require 1..100 unique symbols')
    if any(not isinstance(s,str) or not re.fullmatch(r'[A-Z0-9.\-]{1,20}',s) for s in symbols):
        raise ValueError('Invalid symbol')
    if not math.isfinite(interval) or interval<=0 or not math.isfinite(requests_per_minute) or not 0<requests_per_minute<=600:
        raise ValueError('Invalid interval or client request budget')
    if not isinstance(validity_in_minutes,int) or isinstance(validity_in_minutes,bool) or validity_in_minutes<1:
        raise ValueError('Token validity must be a positive integer of minutes')
    if credential_source not in ('auto','fresh'):
        raise ValueError("credential_source must be 'auto' or 'fresh'")
    detector=dict(threshold=threshold,window=window,min_samples=min_samples,max_age=max_age,max_gap=max_gap)
    VolumeEngine(**detector); EventEngine(threshold,event_gap,cooldown)
    # 'credential_source' rather than 'token_source': the persistence boundary redacts any
    # key containing 'token', which would corrupt checkpoint round-trip comparisons.
    return dict(symbols=symbols,detector=detector,event_gap=event_gap,cooldown=cooldown,
        interval=interval,requests_per_minute=requests_per_minute,max_symbols_per_request=20,
        validity_in_minutes=validity_in_minutes,credential_source=credential_source)


def replay(source,directory, *, resume=False, **options):
    source=Path(source)
    digest=hashlib.sha256();symbols=set()
    with source.open('rb') as stream:
        for line in stream:
            digest.update(line)
            symbols.update(r['symbol'] for r in json.loads(line)['observations'])
    config=configuration(sorted(symbols),**options)
    identity=dict(mode='replay',path=str(source.resolve()),sha256=digest.hexdigest())
    store=ShadowStore(directory,config,resume=resume,source=identity)
    try:
        committed=store.state['transaction']
        with source.open(encoding='utf-8') as stream:
            for index,line in enumerate(stream,1):
                if index<=committed:
                    continue
                record=json.loads(line)
                times=[timestamp(r.get('response_received_at_utc')) for r in record['observations']]
                known=[t for t in times if t]
                store.process(record,processed_at=max(known) if known else datetime(1970,1,1,tzinfo=timezone.utc))
        store.summary('replay_complete')
        # Comparison is an OFFLINE audit, allowed to retain the reference recording.
        records=[json.loads(line) for line in source.open(encoding='utf-8')]
        if any('expected_symbols' in r for r in records):
            reference=VolumeEngine(**config['detector']); rows=[]
            for r in records:
                rows.extend(reference.process(r,expected_symbols=r.get('expected_symbols')))
        else:
            rows=detect(records,**config['detector'])['intervals']
        expected=cluster(rows,config['detector']['threshold'],config['event_gap'],config['cooldown'])
        actual={}
        with (Path(directory)/'events.jsonl').open() as stream:
            for line in stream:
                event=json.loads(line)['event']
                actual[event['event_id'].split(':',1)[1]]=event
        active={s['active']['event_id'] for s in store.events.symbols.values() if s['active']}
        diffs=[]
        for event in expected:
            observed=actual.get(event['event_id'])
            if observed is None:
                diffs.append(dict(event_id=event['event_id'],reason='missing'))
                continue
            for k,v in event.items():
                if k in ('candidate_lines','event_id','right_censored'):
                    continue
                if observed.get(k)!=v:
                    diffs.append(dict(event_id=event['event_id'],field=k))
            if event['right_censored']!=(event['event_id'] in active):
                diffs.append(dict(event_id=event['event_id'],field='right_censored'))
        # Verify every numerical interval, not just aggregate candidate counts.
        interval_mismatches=0
        with (Path(directory)/'intervals.jsonl').open() as stream:
            from itertools import zip_longest
            for expected_row,line in zip_longest(rows,stream):
                observed=json.loads(line) if line else None
                if expected_row is None or observed is None or any(observed.get(k)!=v for k,v in expected_row.items()):
                    interval_mismatches+=1
        comparison=dict(source=identity,config=config,reference_candidates=sum(r['status']=='spike_candidate' for r in rows),
            reference_events=len(expected),online_events=len(actual),reference_alert_opportunities=sum(e['alert_eligible'] for e in expected),
            online_alert_opportunities=store.state['transition_counts'].get('alert_opportunities',0),
            interval_mismatches=interval_mismatches,event_field_mismatches=diffs,
            naturally_closed_events=store.state['transition_counts'].get('close',0),right_censored_events=len(active),
            equivalent=not diffs and interval_mismatches==0 and len(actual)==len(expected),
            finalization='Close is emitted at first subsequent per-symbol observation proving a gap/session/quality break. '
                'No invented end-of-file close. Open events remain resumable and right-censored in summary. '
                'Event IDs have a persistent run prefix; candidate_lines are omitted online to bound memory.')
        atomic_json(Path(directory)/'replay_comparison.json',comparison)
        (Path(directory)/'REPLAY_REPORT.md').write_text('# Phase 3 deterministic replay\n\n'+
            '```json\n'+json.dumps(comparison,indent=2)+'\n```\n',encoding='utf-8')
        return comparison
    except BaseException:
        if not store.poisoned:
            store.summary('replay_failed')
        raise
    finally:
        store.close()


def live(directory,config, *, duration=1800,resume=False,client_factory=None,
         clock=time.monotonic,sleep=time.sleep,wall_clock=utc,install_signals=True,
         timing_diagnostics=False):
    if not math.isfinite(duration) or duration<=0:
        raise ValueError('Duration must be finite and positive')
    # Imports are deferred: offline replay does not load .env or client/config code.
    from public_api_test.multi_batch_stability import monitor_multibatch,divide_batches
    if client_factory is None:
        from public_api_test.client import PublicComClient
        client_factory=PublicComClient
    store=ShadowStore(directory,config,resume=resume,source=dict(mode='live'))
    client=None; stopping=False; auth_failed=False; previous_handlers={}
    def stop(*_):
        nonlocal stopping
        stopping=True
    try:
        if install_signals:
            for sig in (signal.SIGINT,signal.SIGTERM):
                previous_handlers[sig]=signal.signal(sig,stop)
        client=client_factory(requests_per_minute=config['requests_per_minute'],
            validity_in_minutes=config['validity_in_minutes'],
            token_source=config['credential_source'])
        not_before=timestamp(store.state['runtime'].get('not_before_utc'))
        remaining=max(0,(not_before-wall_clock()).total_seconds()) if not_before else 0
        client.next_request=max(client.next_request,clock()+max(remaining,60/config['requests_per_minute'] if resume else 0))
        while clock()<client.next_request and not stopping:
            sleep(min(0.25,client.next_request-clock()))
        if stopping:
            store.summary('interrupted');return True
        setup=client._sanitize_batch(client.prepare())
        if not client.token or not client.account_id:
            store.summary('setup_failed',error=dict(category='authentication_or_account_setup_failed',diagnostics=setup))
            return False
        def consume(record):
            nonlocal auth_failed,stopping
            now=wall_clock()
            runtime=dict(not_before_utc=(now+timedelta(seconds=max(0,client.next_request-clock()))).isoformat())
            commit_timing=None
            if timing_diagnostics:
                commit_timing={};t0=clock();w0=wall_clock().timestamp()
            store.process(record,processed_at=now,runtime=runtime,timing=commit_timing)
            if timing_diagnostics:
                commit_timing['commit_duration_monotonic_ms']=max(0.0,(clock()-t0)*1000)
                commit_timing['wall_monotonic_drift_ms']=((wall_clock().timestamp()-w0)-(clock()-t0))*1000
                store.state['runtime']['last_commit_timing']=dict(commit_timing)
            if any(b.get('http_status') in (401,403) for b in record.get('batches',[])):
                auth_failed=True;stopping=True
        _,interrupted,elapsed,skipped=monitor_multibatch(client,divide_batches(config['symbols'],20),
            config['interval'],duration,lambda record:None,clock=clock,sleep=sleep,wall_clock=wall_clock,
            max_age=config['detector']['max_age'],retain_sweeps=False,on_batch=consume,should_stop=lambda:stopping,
            timing_diagnostics=timing_diagnostics)
        counts=store.volume.counts
        status='authentication_failed' if auth_failed else 'interrupted' if interrupted or stopping else 'completed'
        if status=='completed' and (store.state['transition_counts'].get('request_failure_batches',0) or store.state['transition_counts'].get('partial_or_empty_batches',0)):
            status='degraded'
        if status in ('completed','degraded') and counts.get('valid_intervals',0)==0:
            status='no_valid_intervals'
        store.state['runtime'].update(last_invocation_elapsed_seconds=elapsed,last_invocation_skipped_slots=skipped)
        atomic_json(store.directory/'checkpoint.json',store.state)
        store.summary(status)
        return status in ('completed','interrupted')
    except Exception as error:
        # Class only: exception text can include credentials or URLs.
        if not store.poisoned:
            store.summary('failed',dict(category=type(error).__name__))
        else:
            # Committed checkpoint remains authoritative; do not serialize mutated state.
            atomic_json(store.directory/'failure.json',dict(category=type(error).__name__,requires_resume=True))
        return False
    finally:
        if client:
            client.close()
        for sig,handler in previous_handlers.items():
            signal.signal(sig,handler)
        store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('replay','live'),required=True)
    parser.add_argument('--input',type=Path,help='Saved observations for offline replay')
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--resume',action='store_true')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--symbols',help='Comma-separated universe')
    group.add_argument('--symbols-file',type=Path)
    parser.add_argument('--window',type=int,default=30)
    parser.add_argument('--min-samples',type=int,default=10)
    for name,default in [('threshold',3),('event-gap',30),('cooldown',120),('max-age',60),('max-gap',30),
                         ('interval',10),('duration',1800),('requests-per-minute',40)]:
        parser.add_argument('--'+name,type=float,default=default)
    parser.add_argument('--validity-in-minutes',type=int,default=450,
        help='Requested token lifetime sent as validityInMinutes; acceptance by Public.com is '
             'unverified until the live authentication check succeeds')
    parser.add_argument('--credential-source',choices=('fresh','auto'),default='fresh',
        help='fresh always exchanges PUBLIC_API_KEY for a newly issued token; '
             'auto reuses PUBLIC_ACCESS_TOKEN when present')
    parser.add_argument('--diagnostics-timing',action='store_true',
        help='Record stage timing diagnostics (idle wait, postprocessing, commit substages, '
             'wall/monotonic drift) in batch records and the checkpoint runtime')
    args=vars(parser.parse_args(argv));mode=args.pop('mode');source=args.pop('input')
    directory=args.pop('output_dir');resume=args.pop('resume');duration=args.pop('duration')
    symbols=args.pop('symbols');symbol_file=args.pop('symbols_file')
    timing_diagnostics=args.pop('diagnostics_timing')
    try:
        if mode=='replay':
            if source is None or symbols or symbol_file:
                parser.error('Replay requires --input and infers its universe')
            result=replay(source,directory,resume=resume,**args)
            print('Offline replay equivalent:',result['equivalent'])
            return 0 if result['equivalent'] else 1
        if source:
            parser.error('--input is only for replay')
        symbol_file=symbol_file or Path(__file__).parent/'config'/'scanner_symbols_100.txt'
        universe=[s.strip().upper() for s in (symbols.split(',') if symbols else symbol_file.read_text().splitlines()) if s.strip()]
        config=configuration(universe,**args)
        success=live(directory,config,duration=duration,resume=resume,
            timing_diagnostics=timing_diagnostics)
        print('Shadow session stopped. Inspect',directory/'summary.json')
        return 0 if success else 1
    except (ValueError,OSError) as error:
        print('Shadow scanner could not start or complete:',type(error).__name__,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
