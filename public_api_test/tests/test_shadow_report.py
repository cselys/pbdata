"""Coverage report tests use committed temporary files only."""
import json
from public_api_test.shadow import ShadowStore,configuration
from public_api_test.shadow_report import coverage,render
from public_api_test.tests.test_volume_quality import record,iso


def sample(symbol,t,v,sweep):
    r=record(t,v)
    r['observations'][0]['symbol']=symbol
    r.update(sweep=sweep,expected_symbols=[symbol])
    return r


def test_partial_sweep_persists_across_restart_and_report_is_read_only(tmp_path):
    cfg=configuration(['AAPL','MSFT'],min_samples=2)
    store=ShadowStore(tmp_path,cfg)
    store.process(sample('AAPL',0,0,1))
    store.process(sample('MSFT',1,0,1))
    store.process(sample('AAPL',10,10,2))
    store.summary('interrupted');store.close()
    store=ShadowStore(tmp_path,cfg,resume=True)
    store.process(sample('AAPL',50,50,1))
    store.process(sample('MSFT',51,50,1))
    summary=store.summary('completed');store.close()
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    report=coverage(tmp_path,cp)
    assert report==summary['market_coverage']
    assert report['status']=='incomplete'
    assert report['observed_segments']==2 and report['observed_sweeps']==3
    assert report['missing_observed_sweep_samples']==1
    assert report['partial_sweeps']==[dict(segment=1,sweep=2,missing_symbols=['MSFT'])]
    assert report['sampling_gaps']==2
    assert report['quality_exclusions']['gap_exceeds_limit']==2
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert 'incomplete' in render(report)


def test_empty_and_full_coverage_and_uncommitted_tail(tmp_path):
    cfg=configuration(['AAPL'],min_samples=2)
    store=ShadowStore(tmp_path,cfg)
    assert store.summary('setup_failed')['market_coverage']['status']=='incomplete'
    for i in range(4):store.process(sample('AAPL',i*10,i*10,i+1))
    summary=store.summary('completed');store.close()
    assert summary['market_coverage']['status']=='complete_within_observed_sweeps'
    with (tmp_path/'observations.jsonl').open('ab') as f:f.write(b'partial uncommitted tail')
    cp=json.loads((tmp_path/'checkpoint.json').read_text())
    assert coverage(tmp_path,cp)==summary['market_coverage']


def test_failed_and_stale_quotes_are_not_complete_market_coverage(tmp_path):
    store=ShadowStore(tmp_path,configuration(['AAPL','MSFT'],min_samples=2))
    r=sample('AAPL',0,0,1)
    r['observations'][0].update(category='request_failed',http_status=429)
    store.process(r)
    r=sample('MSFT',100,10,1)
    store.process(r)
    r=sample('MSFT',200,20,2)
    r['observations'][0]['last_timestamp_raw']=sample('MSFT',0,0,1)['observations'][0]['last_timestamp_raw']
    store.process(r)
    report=store.summary('completed')['market_coverage'];store.close()
    assert report['status']=='incomplete'
    assert report['unsuccessful_quote_observations']==1
    assert report['symbols_without_successful_quotes']==['AAPL']
    assert report['quality_exclusions']['stale_last_timestamp']==1


def test_duplicate_samples_do_not_hide_missing_coverage(tmp_path):
    store=ShadowStore(tmp_path,configuration(['AAPL','MSFT'],min_samples=2))
    store.process(sample('AAPL',0,0,1))
    store.process(sample('AAPL',10,10,1))
    report=store.summary('completed')['market_coverage'];store.close()
    assert report['duplicate_symbol_observations_in_sweep']==1
    assert report['missing_observed_sweep_samples']==1
    assert report['per_symbol']['AAPL']['missing_observed_sweep_samples']==0
    assert report['per_symbol']['MSFT']['missing_observed_sweep_samples']==1


def test_sampling_gap_breakdown_by_end_status(tmp_path):
    store=ShadowStore(tmp_path,configuration(['AAPL','MSFT'],min_samples=2))
    store.process(sample('AAPL',0,0,1))
    failed=sample('AAPL',35,5,2);failed['observations'][0].update(category='request_failed',http_status=None)
    store.process(failed)
    store.process(sample('AAPL',70,10,3))
    store.process(sample('MSFT',0,0,4))
    store.process(sample('MSFT',10,10,5))
    store.process(sample('MSFT',45,20,6))
    stale=sample('MSFT',80,30,7);stale['observations'][0]['last_timestamp_raw']=iso(-120)
    store.process(stale)
    report=store.summary('completed')['market_coverage'];store.close()
    assert report['sampling_gaps']==4
    assert report['sampling_gaps_by_end_status']=={'failed_observation':1,'first_valid_observation':1,
        'gap_exceeds_limit':1,'stale_last_timestamp':1}
    assert sum(report['sampling_gaps_by_end_status'].values())==report['sampling_gaps']
    assert report['scheduler_stall_batches']==0
    # Existing metrics unchanged by the new additive fields.
    assert report['unsuccessful_quote_observations']==1
    assert report['interval_status_counts']['gap_exceeds_limit']==1
    assert report['quality_exclusions']['stale_last_timestamp']==1
    text=render(report)
    assert 'Sampling-gap ends by interval status' in text and 'Scheduler stall batches' in text


def test_scheduler_stall_batches_diagnostic(tmp_path):
    store=ShadowStore(tmp_path,configuration(['AAPL'],min_samples=2))
    def batched(t,v,sweep,batches):
        r=sample('AAPL',t,v,sweep);r['batches']=batches;return r
    store.process(batched(0,0,1,[dict(operation_duration_monotonic_ms=24125,http_latency_ms=66)]))
    store.process(batched(10,10,2,[dict(operation_duration_monotonic_ms=3000,http_latency_ms=66)]))
    store.process(batched(20,20,3,[dict(operation_duration_monotonic_ms=None,http_latency_ms=66)]))
    store.process(batched(30,30,4,[dict(operation_duration_monotonic_ms=20000)]))
    store.process(batched(40,40,5,[dict(http_latency_ms=100)]))
    store.process(batched(50,50,6,[dict(operation_duration_monotonic_ms=24060.5,http_latency_ms=60)]))
    store.process(batched(60,60,7,[]))
    report=store.summary('completed')['market_coverage'];store.close()
    assert report['scheduler_stall_batches']==2
    assert report['sampling_gaps']==0 and report['sampling_gaps_by_end_status']=={}


def test_report_tolerates_sessions_without_optional_timing(tmp_path):
    obs=dict(sweep=1,shadow_transaction=1,observations=[dict(symbol='AAPL',category='success',
        http_status=200,response_received_at_utc=iso(0),market_session='regular')])
    inter=dict(symbol='AAPL',received_at_utc=iso(0),status='first_valid_observation')
    (tmp_path/'observations.jsonl').write_text(json.dumps(obs)+'\n',encoding='utf-8')
    (tmp_path/'intervals.jsonl').write_text(json.dumps(inter)+'\n',encoding='utf-8')
    cp=dict(version=1,transaction=1,source=dict(mode='live'),
        config=dict(symbols=['AAPL'],detector=dict(max_gap=30)),
        offsets={'observations.jsonl':(tmp_path/'observations.jsonl').stat().st_size,
                 'intervals.jsonl':(tmp_path/'intervals.jsonl').stat().st_size})
    report=coverage(tmp_path,cp)
    assert report['sampling_gaps']==0
    assert report['sampling_gaps_by_end_status']=={}
    assert report['scheduler_stall_batches']==0
    assert report['observed_sweeps']==1 and report['observations']==1
    assert render(report)
