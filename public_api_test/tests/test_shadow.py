"""Shadow operation uses synthetic observations and mocked HTTP only."""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import public_api_test.multi_batch_stability as multi_batch
import public_api_test.shadow as shadow
from public_api_test.client import PublicComClient
from public_api_test.shadow import ShadowStore, configuration, live, replay
from public_api_test.shadow_report import coverage, render
from public_api_test.volume_spikes import VolumeEngine, detect
from public_api_test.volume_quality import EventEngine, cluster
from public_api_test.tests.test_volume_quality import record, iso, interval
from public_api_test.tests.test_multi_batch_stability import rig, reply, quote, request_symbols, SYMBOLS_100, successful_handler


def config(symbols=None, **kwargs):
    return configuration(symbols or ['AAPL'],min_samples=2,**kwargs)


def observations():
    return [record(t,v) for t,v in [(0,0),(10,10),(20,20),(30,100),(40,200),(80,210),(90,400),(200,410)]]


def test_shared_incremental_calculations_and_event_rules_match():
    records=observations()
    expected=detect(records,min_samples=2)
    engine=VolumeEngine(min_samples=2)
    actual=[]
    for i,r in enumerate(records):
        actual.extend(engine.process(r))
        engine=VolumeEngine(min_samples=2,state=json.loads(json.dumps(engine.state())))
    assert actual==expected['intervals']
    shared=EventEngine();latest={}
    for row in actual:
        for change in shared.process(row):
            latest[change['event']['event_id']]=change['event']
        shared=EventEngine(state=json.loads(json.dumps(shared.symbols)))
    expected_events=cluster(actual)
    for event in expected_events:
        assert {k:v for k,v in event.items() if k not in ('candidate_lines','right_censored')} == {
            k:v for k,v in latest[event['event_id']].items() if k!='right_censored'}


def test_restart_cooldown_no_duplicate_event_and_bounded_state(tmp_path):
    cfg=config(max_gap=1000)
    records=observations()
    store=ShadowStore(tmp_path,cfg)
    for r in records[:5]:
        store.process(r)
    event_id=store.events.symbols['AAPL']['active']['event_id']
    store.close()
    store=ShadowStore(tmp_path,cfg,resume=True)
    assert store.events.symbols['AAPL']['active']['event_id']==event_id
    for r in records[5:]:
        store.process(r)
    transitions=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    openings=[c for c in transitions if c['transition']=='open']
    assert len(openings)==2 and openings[1]['event']['cooldown_suppressed']
    assert len({c['transition_id'] for c in transitions})==len(transitions)
    for i in range(75):
        store.process(record(300+i*10,1000+i*10))
    assert len(store.volume.history['AAPL'])<=30
    assert len(store.events.symbols)==1
    assert all('candidate_lines' not in c['event'] for c in transitions)
    assert (tmp_path/'checkpoint.json').stat().st_size<15000
    store.close()


def test_failed_commit_rolls_back_all_output_tails(tmp_path,monkeypatch):
    import public_api_test.shadow as module
    cfg=config();store=ShadowStore(tmp_path,cfg)
    for r in observations()[:3]:
        store.process(r)
    before={name:(tmp_path/name).read_bytes() for name in module.FILES}
    real=module.atomic_json
    def fail_checkpoint(path,value):
        if path.name=='checkpoint.json':
            raise OSError('simulated disk failure')
        real(path,value)
    monkeypatch.setattr(module,'atomic_json',fail_checkpoint)
    with pytest.raises(OSError):
        store.process(observations()[3])
    assert store.poisoned
    store.close();monkeypatch.setattr(module,'atomic_json',real)
    store=ShadowStore(tmp_path,cfg,resume=True)
    assert store.state['transaction']==3
    for name,content in before.items():
        assert (tmp_path/name).read_bytes()==content
    store.process(observations()[3]);store.close()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert len(events)==1 and events[0]['transition']=='open'


def test_missing_timestamp_zero_burst_and_missing_symbol(tmp_path):
    store=ShadowStore(tmp_path,config())
    for r in [record(0,0),record(10,10),record(20,20),record(30,20),record(40,200)]:
        intervals,_=store.process(r)
    assert intervals[0]['zero_then_burst_suspected']
    assert intervals[0]['prior_zero_intervals']==1
    intervals,changes=store.process(record(50,210,last_timestamp_raw=None))
    assert intervals[0]['status']=='missing_or_invalid_last_timestamp'
    assert changes[0]['transition']=='close'
    store.process({'observations':[]})
    assert store.volume.counts['missing_observation']==1
    store.close()


def test_replay_resume_is_idempotent_and_exact(tmp_path):
    raw=tmp_path/'input.jsonl';raw.write_text(''.join(json.dumps(r)+'\n' for r in observations()))
    out=tmp_path/'out';before=raw.read_bytes()
    result=replay(raw,out,min_samples=2)
    assert result['equivalent'] and result['interval_mismatches']==0
    snapshots={name:(out/name).read_bytes() for name in ('events.jsonl','events.csv','intervals.jsonl')}
    again=replay(raw,out,resume=True,min_samples=2)
    assert again==result
    assert raw.read_bytes()==before
    for name,value in snapshots.items():
        assert (out/name).read_bytes()==value


def test_resume_config_and_truncated_output_fail_closed(tmp_path):
    store=ShadowStore(tmp_path,config());store.process(record(0,0));store.close()
    with pytest.raises(ValueError,match='same schema'):
        ShadowStore(tmp_path,config(threshold=4),resume=True)
    (tmp_path/'observations.jsonl').write_bytes(b'')
    with pytest.raises(ValueError,match='truncated'):
        ShadowStore(tmp_path,config(),resume=True)


def test_second_writer_rejected_and_output_sanitized(tmp_path):
    store=ShadowStore(tmp_path,config())
    with pytest.raises(OSError):
        ShadowStore(tmp_path,config(),resume=True)
    r=record(0,0,raw_response={'accessToken':'SECRET','accountId':'PRIVATE'},
             endpoint_url='https://example/marketdata/PRIVATE/quotes')
    store.process(r);store.close()
    for path in tmp_path.iterdir():
        if path.suffix in ('.json','.jsonl','.csv'):
            assert 'SECRET' not in path.read_text() and 'PRIVATE' not in path.read_text()


def test_streaming_collector_budget_and_instrumentation(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr('public_api_test.client.time.perf_counter',clock.tick)
    def handler(method,url,**kwargs):
        clock.sleep(.08)
        return reply([quote(s,stamp=clock.utc().isoformat()) for s in request_symbols(kwargs)])
    client.session.request.side_effect=handler
    cfg=config(SYMBOLS_100)
    assert live(tmp_path,cfg,duration=21,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert client.session.request.call_count==15
    client.prepare.assert_called_once()
    logs=[json.loads(line) for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert len(logs)==15 and all(len(r['observations'])==20 for r in logs)
    starts=[__import__('datetime').datetime.fromisoformat(r['batches'][0]['request_start_utc']) for r in logs]
    assert all((b-a).total_seconds()>=1.5 for a,b in zip(starts,starts[1:]))
    assert logs[0]['batches'][0]['operation_duration_monotonic_ms']==pytest.approx(80)
    assert logs[0]['batches'][0]['http_latency_ms']==pytest.approx(80)
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['interval_counts']['observations']==300
    assert summary['interval_counts'].get('missing_observation',0)==0
    assert 'account-value' not in (tmp_path/'observations.jsonl').read_text()


def test_429_timeout_recover_without_retry_burst(tmp_path,rig):
    client,clock=rig; client.prepare=Mock(return_value=[])
    calls=[]
    def handler(method,url,**kwargs):
        calls.append(clock.tick())
        if len(calls)==1:
            response=reply(status=429);response.headers={'Retry-After':'4'};return response
        if len(calls)==2:
            raise requests.Timeout('secret must not be logged')
        return reply([quote(s,stamp=clock.utc().isoformat()) for s in request_symbols(kwargs)])
    client.session.request.side_effect=handler
    assert not live(tmp_path,config(SYMBOLS_100),duration=11,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert calls[1]-calls[0]>=4 and all(b-a>=1.5 for a,b in zip(calls,calls[1:]))
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['transition_counts']['http_status_429']==1
    assert summary['transition_counts']['request_category_network_error']==1
    assert summary['status']=='degraded'
    assert 'secret must not be logged' not in (tmp_path/'observations.jsonl').read_text()


def test_auth_failure_stops_after_first_batch(tmp_path,rig):
    client,clock=rig;client.prepare=Mock(return_value=[])
    client.session.request.return_value=reply(status=401);client.session.request.side_effect=None
    assert not live(tmp_path,config(SYMBOLS_100),duration=300,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert client.session.request.call_count==1
    assert json.loads((tmp_path/'summary.json').read_text())['status']=='authentication_failed'


def test_persisted_backoff_respected_before_resume_setup(tmp_path,rig):
    client,clock=rig;cfg=config()
    store=ShadowStore(tmp_path,cfg,source={'mode':'live'})
    store.process(record(0,0),runtime={'not_before_utc':(clock.utc()+__import__('datetime').timedelta(seconds=20)).isoformat()})
    store.close()
    observed=[]
    client.prepare=Mock(side_effect=lambda:observed.append(clock.tick()) or [])
    live(tmp_path,cfg,duration=1,resume=True,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert observed[0]>=20


def test_graceful_signal_commits_current_batch(tmp_path,rig,monkeypatch):
    import public_api_test.shadow as module
    client,clock=rig;client.prepare=Mock(return_value=[])
    handlers={}
    def register(sig,handler):
        old=handlers.get(sig)
        handlers[sig]=handler
        return old
    monkeypatch.setattr(module.signal,'signal',register)
    calls=0
    def handler(method,url,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:
            handlers[module.signal.SIGINT](module.signal.SIGINT,None)
        return reply([quote(s,stamp=clock.utc().isoformat()) for s in request_symbols(kwargs)])
    client.session.request.side_effect=handler
    assert live(tmp_path,config(SYMBOLS_100),duration=300,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc)
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['status']=='interrupted' and summary['committed_transactions']==2
    assert client.session.request.call_count==2


def test_unbounded_continuous_event_has_bounded_state():
    engine=EventEngine()
    for i in range(1,2001):
        engine.process(interval(i*10))
    assert engine.symbols['AAPL']['active']['candidate_count']==2000
    assert len(json.dumps(engine.symbols))<2000


def test_negative_volume_diagnostics(tmp_path):
    store=ShadowStore(tmp_path,config())
    store.process(record(0,100))
    rows,_=store.process(record(10,90))
    assert rows[0]['volume_delta'] is None
    assert rows[0]['rejected_negative_volume_change']==-10
    rows,_=store.process(record(20,-1))
    assert rows[0]['raw_cumulative_volume']==-1
    assert rows[0]['volume_observation_quality']=='negative'
    assert rows[0]['volume_delta'] is None
    store.close()


def test_resume_inside_unexpired_cooldown_with_valid_baseline(tmp_path):
    """A lost cooldown changes this decision: restart occurs before its expiry."""
    cfg=config(window=3,event_gap=10,cooldown=120)
    store=ShadowStore(tmp_path,cfg)
    for t,v in [(0,0),(10,10),(20,20),(30,100),(40,110),(50,120)]:
        store.process(record(t,v))
    assert store.events.symbols['AAPL']['last_alert']==iso(30)
    assert store.events.symbols['AAPL']['active'] is None
    before=store.volume.state()
    store.close()
    store=ShadowStore(tmp_path,cfg,resume=True)
    assert json.loads(json.dumps(store.volume.state()))==json.loads(json.dumps(before))
    assert store.events.symbols['AAPL']['last_alert']==iso(30)
    reset_state=json.loads(json.dumps(store.events.symbols))
    reset_state['AAPL']['last_alert']=None
    rows,changes=store.process(record(60,220))
    assert rows[0]['status']=='spike_candidate'
    assert rows[0]['baseline_sample_count']==3  # no gap reset or restart warm-up
    opened=[c['event'] for c in changes if c['transition']=='open']
    assert len(opened)==1 and opened[0]['cooldown_suppressed'] and not opened[0]['alert_eligible']
    assert store.events.symbols['AAPL']['last_alert']==iso(30)  # suppression does not extend cooldown
    # Negative control: the very same observation would be eligible if restore lost cooldown.
    reset=EventEngine(3,10,120,state=reset_state)
    assert reset.process(rows[0])[-1]['event']['alert_eligible']
    for t in range(70,150,10):
        store.process(record(t,220+(t-60)))
    _,changes=store.process(record(150,400))
    assert changes[-1]['transition']=='open' and changes[-1]['event']['alert_eligible']
    assert store.events.symbols['AAPL']['last_alert']==iso(150)  # exactly 120 seconds
    store.close()


AUTH_TOKEN='fresh-token-value'


def auth_response(status=200):
    response=Mock(status_code=status,headers={})
    response.json.return_value=(dict(accessToken=AUTH_TOKEN) if status==200
        else dict(error='Unauthorized'))
    return response


def account_response():
    response=Mock(status_code=200,headers={})
    response.json.return_value=dict(accounts=[dict(accountId='account-value')])
    return response


def quote_handler(clock):
    """Serves the token exchange, the account lookup and quote POSTs on one transport."""
    def handler(method,url,**kwargs):
        if 'access-tokens' in url:
            return auth_response()
        if 'trading/account' in url:
            return account_response()
        return reply([quote(s,stamp=clock.utc().isoformat()) for s in request_symbols(kwargs)])
    return handler


def test_token_configuration_defaults_and_validation():
    assert config()['validity_in_minutes']==450 and config()['credential_source']=='fresh'
    assert configuration(['AAPL'],min_samples=2,validity_in_minutes=90)['validity_in_minutes']==90
    for bad in (0,-1,2.5,'450',True):
        with pytest.raises(ValueError):
            configuration(['AAPL'],min_samples=2,validity_in_minutes=bad)
    with pytest.raises(ValueError):
        configuration(['AAPL'],min_samples=2,credential_source='environment')


def test_client_validates_token_parameters():
    with pytest.raises(ValueError):
        PublicComClient(validity_in_minutes=0)
    with pytest.raises(ValueError):
        PublicComClient(validity_in_minutes=10.5)
    with pytest.raises(ValueError):
        PublicComClient(token_source='refresh')


def test_auto_source_reuses_environment_token_without_exchange(monkeypatch):
    monkeypatch.setattr('public_api_test.client.time.sleep',lambda s:None)
    monkeypatch.setenv('PUBLIC_ACCESS_TOKEN','existing-token-value')
    monkeypatch.setenv('PUBLIC_ACCOUNT_ID','account-value')
    client=PublicComClient(api_key='secret-value')
    client.session.request=Mock()
    assert client.token=='existing-token-value'
    assert client.prepare()==[] and client.session.request.call_count==0
    client.close()


def test_fresh_source_exchanges_secret_with_configured_validity(monkeypatch):
    monkeypatch.setattr('public_api_test.client.time.sleep',lambda s:None)
    monkeypatch.setenv('PUBLIC_ACCESS_TOKEN','stale-env-token')
    monkeypatch.setenv('PUBLIC_ACCOUNT_ID','account-value')
    client=PublicComClient(api_key='secret-value',validity_in_minutes=450,token_source='fresh')
    client.session.request=Mock(return_value=auth_response())
    assert client.token is None  # the environment token is never adopted in fresh mode
    records=client.prepare()
    assert records[0]['category']=='success' and client.token==AUTH_TOKEN
    assert client.session.request.call_args.kwargs['json']=={'secret':'secret-value','validityInMinutes':450}
    client.close()


def test_custom_validity_reaches_exchange_body(monkeypatch):
    monkeypatch.setattr('public_api_test.client.time.sleep',lambda s:None)
    monkeypatch.setenv('PUBLIC_API_KEY','secret-value')
    monkeypatch.setenv('PUBLIC_ACCOUNT_ID','account-value')
    client=PublicComClient(validity_in_minutes=90,token_source='fresh')
    client.session.request=Mock(return_value=auth_response())
    client.prepare()
    assert client.session.request.call_args.kwargs['json']['validityInMinutes']==90
    client.close()


def test_fresh_without_secret_never_uses_environment_token(monkeypatch):
    monkeypatch.setattr('public_api_test.client.time.sleep',lambda s:None)
    monkeypatch.setenv('PUBLIC_ACCESS_TOKEN','existing-token-value')
    client=PublicComClient(api_key=None,token_source='fresh')
    client.session.request=Mock()
    records=client.prepare()
    assert records[0]['category']=='configuration_error'
    assert 'PUBLIC_ACCESS_TOKEN is not reused' in records[0]['api_error_message']
    assert client.token is None and client.session.request.call_count==0
    client.close()


def test_live_forwards_token_configuration_end_to_end(tmp_path,rig,monkeypatch):
    _,clock=rig
    monkeypatch.setenv('PUBLIC_API_KEY','secret-value')
    seen={};exchanges=[]
    def factory(**kw):
        seen.update(kw)
        real=PublicComClient(**kw)
        def handler(method,url,**kwargs):
            if 'access-tokens' in url:
                exchanges.append(kwargs.get('json'))
            return quote_handler(clock)(method,url,**kwargs)
        real.session.request=Mock(side_effect=handler)
        return real
    assert live(tmp_path,config(SYMBOLS_100),duration=21,client_factory=factory,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert seen=={'requests_per_minute':40,'validity_in_minutes':450,'token_source':'fresh'}
    assert exchanges==[{'secret':'secret-value','validityInMinutes':450}]
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['status']=='completed' and summary['config']['validity_in_minutes']==450
    assert summary['config']['credential_source']=='fresh'  # survives the redaction boundary
    persisted=''.join(p.read_text() for p in tmp_path.iterdir() if p.suffix in ('.json','.jsonl','.csv'))
    assert 'secret-value' not in persisted and AUTH_TOKEN not in persisted


def test_401_after_successful_exchange_stops_without_retry(tmp_path,rig,monkeypatch):
    _,clock=rig
    monkeypatch.setenv('PUBLIC_API_KEY','secret-value')
    quotes=[]
    def factory(**kw):
        real=PublicComClient(**kw)
        def handler(method,url,**kwargs):
            if 'access-tokens' in url:
                return auth_response()
            if 'trading/account' in url:
                return account_response()
            quotes.append(clock.tick())
            return reply(status=401)
        real.session.request=Mock(side_effect=handler)
        return real
    assert not live(tmp_path,config(SYMBOLS_100),duration=21,client_factory=factory,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    assert len(quotes)==1  # one 401 stops the scanner; the failed request is never retried
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['status']=='authentication_failed'
    assert summary['committed_transactions']==1 and summary['transition_counts']['http_status_401']==1
    persisted=''.join(p.read_text() for p in tmp_path.iterdir() if p.suffix in ('.json','.jsonl','.csv'))
    assert 'secret-value' not in persisted and AUTH_TOKEN not in persisted


def test_live_resume_continues_transaction_numbering(tmp_path,rig):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    def handler(method,url,**kwargs):
        return reply([quote(s,volume=100+int(clock.tick()),stamp=clock.utc().isoformat())
            for s in request_symbols(kwargs)])
    client.session.request.side_effect=handler
    cfg=config(SYMBOLS_100)
    assert live(tmp_path,cfg,duration=21,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    first=json.loads((tmp_path/'summary.json').read_text())
    client2=PublicComClient(api_key='secret-value',account_id='account-value',requests_per_minute=40)
    client2.token='token-value';client2.prepare=Mock(return_value=[])
    client2.session.request=Mock(side_effect=handler)
    assert live(tmp_path,cfg,duration=21,resume=True,client_factory=lambda **kw:client2,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    second=json.loads((tmp_path/'summary.json').read_text())
    rows=[json.loads(line) for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert second['committed_transactions']==len(rows)==2*first['committed_transactions']
    assert [r['shadow_transaction'] for r in rows]==list(range(1,len(rows)+1))
    transitions=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert len({t['transition_id'] for t in transitions})==len(transitions)
    intervals=[json.loads(line) for line in (tmp_path/'intervals.jsonl').read_text().splitlines()]
    assert all(i['transaction']<=len(rows) for i in intervals)


def test_resume_of_session_recorded_before_token_configuration(tmp_path):
    cfg=config()
    store=ShadowStore(tmp_path,cfg,source={'mode':'live'})
    store.process(record(0,0));store.close()
    state=json.loads((tmp_path/'checkpoint.json').read_text())
    for key in ('validity_in_minutes','credential_source'):
        state['config'].pop(key,None)
    (tmp_path/'checkpoint.json').write_text(json.dumps(state))
    store=ShadowStore(tmp_path,cfg,source={'mode':'live'},resume=True)
    store.process(record(10,10));store.close()
    rows=[json.loads(line) for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert len(rows)==2 and rows[1]['shadow_transaction']==2


def _live_timed(tmp_path,client,clock,**kwargs):
    kwargs.setdefault('wall_clock',clock.utc)
    return live(tmp_path,config(SYMBOLS_100),duration=21,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,install_signals=False,
        timing_diagnostics=True,**kwargs)


def test_timing_disabled_mode_leaves_no_trace(tmp_path,rig):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    assert live(tmp_path,config(SYMBOLS_100),duration=21,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    rows=[json.loads(line) for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert len(rows)==15
    assert all(not any(k in r['batches'][0] for k in ('idle_wait_monotonic_ms',
        'postprocess_duration_monotonic_ms','wall_monotonic_drift_ms')) for r in rows)
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    assert 'last_commit_timing' not in cp['runtime']
    assert coverage(tmp_path,cp)['timing']=={}


def test_timing_postprocess_injection(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    real_cycle=multi_batch.cycle_observations
    def slow_cycle(*a,**kw):
        clock.sleep(0.1)
        return real_cycle(*a,**kw)
    monkeypatch.setattr(multi_batch,'cycle_observations',slow_cycle)
    assert _live_timed(tmp_path,client,clock)
    batches=[json.loads(line)['batches'][0] for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert len(batches)==15
    for b in batches:
        assert b['postprocess_duration_monotonic_ms']==pytest.approx(100,abs=1)
        assert b['operation_duration_monotonic_ms']==pytest.approx(0,abs=1)
        assert b['wall_monotonic_drift_ms']==pytest.approx(0,abs=1)
        assert b['idle_wait_monotonic_ms']>=0


def test_timing_commit_substage_injection(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    real_atomic=shadow.atomic_json
    def slow_atomic(path,value):
        clock.sleep(0.5)
        real_atomic(path,value)
    monkeypatch.setattr(shadow,'atomic_json',slow_atomic)
    assert _live_timed(tmp_path,client,clock)
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    timing=cp['runtime']['last_commit_timing']
    assert timing['commit_duration_monotonic_ms']==pytest.approx(1000,abs=1)
    assert timing['checkpoint_write_ms']==pytest.approx(500,abs=1)
    assert timing['summary_write_ms']==pytest.approx(500,abs=1)
    assert timing['append_fsync_ms']==pytest.approx(0,abs=1)
    assert timing['other_commit_ms']==pytest.approx(0,abs=1)
    assert timing['wall_monotonic_drift_ms']==pytest.approx(0,abs=1)


def test_timing_long_stall_single_stage(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    real_cycle=multi_batch.cycle_observations
    calls={'n':0}
    def stall_cycle(*a,**kw):
        calls['n']+=1
        if calls['n']==7:
            clock.sleep(6.0)
        return real_cycle(*a,**kw)
    monkeypatch.setattr(multi_batch,'cycle_observations',stall_cycle)
    assert _live_timed(tmp_path,client,clock)
    batches=[json.loads(line)['batches'][0] for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert len(batches)==10  # the 6 s stall consumes one schedule slot
    assert batches[6]['postprocess_duration_monotonic_ms']==pytest.approx(6000,abs=1)
    for index,b in enumerate(batches):
        if index!=6:
            assert b['postprocess_duration_monotonic_ms']==pytest.approx(0,abs=1)
        assert b['operation_duration_monotonic_ms']==pytest.approx(0,abs=1)
    assert batches[7]['idle_wait_monotonic_ms']==pytest.approx(0,abs=0.1)
    report=json.loads((tmp_path/'summary.json').read_text())
    assert report['status']=='completed'
    assert report['market_coverage']['scheduler_stall_batches']==0
    assert report['market_coverage']['timing']['postprocess_duration_monotonic_ms']['maximum']==pytest.approx(6000,abs=1)


def test_timing_wall_clock_jump_detected(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    jump={'on':False}
    def wall():
        moment=clock.utc()
        return moment+timedelta(seconds=2.5) if jump['on'] else moment
    calls={'n':0}
    def handler(method,url,**kwargs):
        calls['n']+=1
        if calls['n']==15:
            jump['on']=True
        return reply([quote(s,stamp=clock.utc().isoformat()) for s in request_symbols(kwargs)])
    client.session.request=Mock(side_effect=handler)
    assert _live_timed(tmp_path,client,clock,wall_clock=wall)
    batches=[json.loads(line)['batches'][0] for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert batches[-1]['wall_monotonic_drift_ms']==pytest.approx(2500,abs=50)
    assert all(b['wall_monotonic_drift_ms']==pytest.approx(0,abs=50) for b in batches[:-1])
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    assert cp['runtime']['last_commit_timing']['wall_monotonic_drift_ms']==pytest.approx(0,abs=50)
    report=json.loads((tmp_path/'summary.json').read_text())
    assert report['status']=='completed'
    assert report['market_coverage']['sampling_gaps']==0


def test_timing_resume_from_undiagnosed_checkpoint(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    cfg=config(SYMBOLS_100)
    assert live(tmp_path,cfg,duration=21,client_factory=lambda **kw:client,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False)
    client2=PublicComClient(api_key='secret-value',account_id='account-value',requests_per_minute=40)
    client2.token='token-value';client2.prepare=Mock(return_value=[])
    client2.session.request=Mock(side_effect=successful_handler)
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    assert live(tmp_path,cfg,duration=21,resume=True,client_factory=lambda **kw:client2,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc,install_signals=False,timing_diagnostics=True)
    rows=[json.loads(line) for line in (tmp_path/'observations.jsonl').read_text().splitlines()]
    assert [r['shadow_transaction'] for r in rows]==list(range(1,31))
    timed=[r for r in rows if 'idle_wait_monotonic_ms' in r['batches'][0]]
    assert len(timed)==15
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    assert 'last_commit_timing' in cp['runtime']


def test_timing_never_negative_and_exception_safe(tmp_path,monkeypatch):
    fake={'t':100.0}
    monkeypatch.setattr(shadow.time,'monotonic',lambda:fake['t'])
    real_append=ShadowStore._append
    def backward_append(self,name,payload):
        fake['t']-=0.2
        return real_append(self,name,payload)
    monkeypatch.setattr(ShadowStore,'_append',backward_append)
    store=ShadowStore(tmp_path,config())
    timing={}
    store.process(record(0,0),timing=timing)
    assert timing['append_fsync_ms']==0.0
    assert all(value>=0 for value in timing.values())
    store.close()
    monkeypatch.setattr(ShadowStore,'_append',real_append)
    real_atomic=shadow.atomic_json
    def fail_atomic(path,value):
        if path.name=='checkpoint.json':
            raise OSError('simulated disk failure')
        real_atomic(path,value)
    store2=ShadowStore(tmp_path/'second',config())
    monkeypatch.setattr(shadow,'atomic_json',fail_atomic)
    timing2={}
    with pytest.raises(OSError):
        store2.process(record(0,0),timing=timing2)
    assert store2.poisoned  # in-memory transaction bumps before checkpoint; committed file stays at 0
    assert json.loads((tmp_path/'second'/'checkpoint.json').read_text())['transaction']==0
    assert timing2.get('append_fsync_ms',0.0)>=0
    assert 'checkpoint_write_ms' not in timing2  # missing stays missing, never fabricated as zero
    store2.close()


def test_cli_propagates_diagnostics_timing(tmp_path,monkeypatch):
    captured={}
    def fake_live(directory,config,**kw):
        captured.update(kw)
        return True
    monkeypatch.setattr(shadow,'live',fake_live)
    base=['--mode','live','--output-dir',str(tmp_path),'--interval','10','--duration','5']
    assert shadow.main(base+['--diagnostics-timing'])==0
    assert captured['timing_diagnostics'] is True
    assert shadow.main(base)==0
    assert captured['timing_diagnostics'] is False


def test_timing_fields_appear_in_coverage_report(tmp_path,rig,monkeypatch):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    monkeypatch.setattr(shadow.time,'monotonic',clock.tick)
    real_cycle=multi_batch.cycle_observations
    def slow_cycle(*a,**kw):
        clock.sleep(0.1)
        return real_cycle(*a,**kw)
    monkeypatch.setattr(multi_batch,'cycle_observations',slow_cycle)
    assert _live_timed(tmp_path,client,clock)
    report=json.loads((tmp_path/'summary.json').read_text())['market_coverage']
    assert report['timing']['postprocess_duration_monotonic_ms']['count']==15
    assert report['timing']['postprocess_duration_monotonic_ms']['maximum']==pytest.approx(100,abs=1)
    assert report['timing']['idle_wait_monotonic_ms']['count']==15
    assert report['timing']['wall_monotonic_drift_ms']['maximum']==pytest.approx(0,abs=1)
    assert report['timing_outliers']==[]
    assert 'Timing diagnostics' in render(report) and 'Aggregates:' in render(report)
