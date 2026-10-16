from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from public_api_test.volume_spikes import detect
from public_api_test.volume_quality import (
    analyze_prices, cluster, price_rows, quality_diagnostics, response, run, sensitivity,
)

START = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)


def iso(seconds):
    return (START+timedelta(seconds=seconds)).isoformat()


def interval(t, rvol=4, delta=40, line=None, **changes):
    row = dict(symbol='AAPL', input_line=line or int(t), received_at_utc=iso(t),
        previous_received_at_utc=iso(t-10), last_timestamp_raw=iso(t), last_age_seconds=0,
        market_session='regular', volume_delta=delta, elapsed_seconds=10, rvol=rvol,
        baseline_rate=1, baseline_sample_count=10, baseline_method='prior weighted',
        status='spike_candidate' if rvol is not None and rvol >= 3 else 'below_threshold')
    row.update(changes)
    return row


def record(t, v, price=100, **changes):
    row=dict(symbol='AAPL',category='success',http_status=200,volume=v,price=price,
        response_received_at_utc=iso(t),last_timestamp_raw=iso(t),market_session='regular')
    row.update(changes)
    return dict(observations=[row])


def test_event_gap_volume_span_and_cooldown():
    rows=[interval(10), interval(20,1,10), interval(30,6,60), interval(70), interval(150)]
    events=cluster(rows,event_gap=30,cooldown=120)
    assert len(events)==3
    a,b,c=events
    assert a['decision_time']==iso(10)
    assert a['end']==iso(30) and a['peak_rvol']==6
    assert a['total_volume_delta']==110 and a['candidate_volume_delta']==100
    assert a['candidate_count']==2 and a['duration_seconds']==20 and a['volume_span_seconds']==30
    assert a['alert_eligible'] and b['cooldown_suppressed'] and c['alert_eligible']
    # Retrospective peak cannot change the original decision or cooldown decision.
    prefix=cluster(rows[:1])[0]
    assert prefix['decision_time']==a['decision_time'] and prefix['decision_rvol']==a['decision_rvol']


def test_quality_break_and_session_boundary_split_events():
    rows=[interval(10),interval(20,None,None),interval(30),interval(40,market_session='afterhours')]
    events=cluster(rows)
    assert len(events)==3
    assert events[-1]['alert_eligible']


def test_gap_boundary_inclusive_and_symbols_separate():
    rows=[interval(10),interval(40),interval(41,symbol='MSFT')]
    assert len(cluster(rows,event_gap=30))==2
    assert len(cluster(rows,event_gap=29))==3


def test_actual_price_horizon_and_sampled_excursions():
    rows=price_rows([record(t,t,p) for t,p in [(0,100),(12,99),(28,103),(35,102)]],60)['AAPL']
    result=response(rows,1,30,tolerance=5)
    assert result['status']=='available'
    assert result['actual_elapsed_seconds']==35
    assert result['signed_return_pct']==pytest.approx(2)
    assert result['absolute_return_pct']==pytest.approx(2)
    assert result['max_up_return_pct']==pytest.approx(3)
    assert result['max_down_return_pct']==pytest.approx(-1)
    assert response(rows,1,30,tolerance=4)['status']=='horizon_tolerance_exceeded'
    assert response(rows,1,60)['status']=='recording_ended_before_horizon'
    assert response(rows,1,30,next_event=START+timedelta(seconds=34))['status']=='overlapping_separate_event'


@pytest.mark.parametrize('change,reason', [
    ({'price':None},'invalid_price_in_path'),
    ({'last_timestamp_raw':iso(-100)},'invalid_price_in_path'),
    ({'market_session':'afterhours'},'session_boundary'),
    ({'last_timestamp_raw':iso(0)},'no_post_decision_trade_timestamp'),
])
def test_missing_price_quality_and_session(change,reason):
    rows=price_rows([record(0,0),record(10,10),record(30,30,**change)],60)['AAPL']
    assert response(rows,1,30)['status']==reason


def test_price_path_gap():
    rows=price_rows([record(0,0),record(40,40)],60)['AAPL']
    assert response(rows,1,30)['status']=='price_path_gap_or_non_increasing_time'


def matching_fixture():
    records=[record(t,t,100+t/10000) for t in range(0,611,10)]
    rows=[interval(t,1,10,line=i+1) for i,t in enumerate(range(0,611,10))]
    rows[30].update(rvol=4,status='spike_candidate')
    return records,rows,cluster(rows)


def test_control_selection_is_prior_only_no_reuse_or_event_contamination():
    records,rows,events=matching_fixture()
    result=analyze_prices(records,rows,events)
    match=result['matches'][0]
    assert match['status']=='matched'
    assert match['control_time']==iso(160)  # latest with 135s follow-up before t=300
    for o in result['outcomes']:
        assert o['control']['endpoint_time'] < events[0]['decision_time']
    # Future prices cannot alter selection; neither can a later candidate.
    changed_records=records[:31]+[record(t,t,999) for t in range(310,611,10)]
    changed_rows=[dict(r) for r in rows]
    changed_rows[31].update(rvol=100,status='spike_candidate')
    changed=analyze_prices(changed_records,changed_rows,cluster(changed_rows))
    assert changed['matches'][0]==match
    # Adding another event cannot reuse an overlapping control path.
    rows[45].update(rvol=4,status='spike_candidate')
    extended=analyze_prices(records,rows,cluster(rows))
    matched=[m for m in extended['matches'] if m['status']=='matched']
    assert len({m['control_time'] for m in matched})==len(matched)
    times=sorted(datetime.fromisoformat(m['control_time']) for m in matched)
    assert all((b-a).total_seconds()>135 for a,b in zip(times,times[1:]))


def test_matching_does_not_select_on_outcome_availability():
    records,rows,events=matching_fixture()
    records[20]['observations'][0]['price']=None
    result=analyze_prices(records,rows,events)
    assert result['matches'][0]['control_time']==iso(160)
    assert result['outcomes'][1]['control']['status']=='invalid_price_in_path'


def test_zero_then_large_is_suspected_not_confirmed():
    rows=[interval(10,1,0),interval(20,1,0),interval(30,9,90),interval(40,None,None),interval(50,4,40)]
    report=quality_diagnostics(rows)
    assert report['zero_delta_intervals']==2
    assert report['suspected_delayed_reporting_count']==1
    assert report['zero_then_large_delta'][0]['prior_zero_intervals']==2
    assert report['confirmed_anomalies']==0


def test_sensitivity_denominators_and_phase2a_compatibility():
    records=[record(t,t) for t in range(0,1100,10)]
    reports,base=sensitivity(records,min_samples=10,max_age=60,max_gap=30,event_gap=30,cooldown=120)
    assert len(reports)==24
    assert base==detect(records)
    fixed=[r for r in reports if r['warmup_policy']=='fixed_minimum']
    full=[r for r in reports if r['warmup_policy']=='full_window']
    assert {r['eligible_observations'] for r in fixed}=={99}
    assert {r['eligible_observations'] for r in full}=={79,49,19}
    assert {r['common_eligible_observations'] for r in reports}=={19}


def test_output_privacy_input_preservation_and_empty_events(tmp_path):
    source=tmp_path/'raw.jsonl'
    records=[record(t,t,accessToken='SECRET',accountId='PRIVATE') for t in range(0,1000,10)]
    source.write_text(''.join(json.dumps(r)+'\n' for r in records))
    before=source.read_bytes()
    report,comparisons,prices=run(source,tmp_path/'out')
    assert report['event_count']==0
    assert source.read_bytes()==before
    assert report['input']['sha256']==hashlib.sha256(before).hexdigest()
    assert len(list((tmp_path/'out').iterdir()))==8
    for path in (tmp_path/'out').iterdir():
        assert 'SECRET' not in path.read_text() and 'PRIVATE' not in path.read_text()
    assert prices['summary']['30']['paired_count']==0


def test_invalid_config(tmp_path):
    with pytest.raises(ValueError):
        run(tmp_path/'missing',tmp_path,min_samples=31)
    with pytest.raises(ValueError):
        run(tmp_path/'missing',tmp_path,event_gap=float('nan'))


def test_nonempty_event_exports(tmp_path):
    source=tmp_path/'raw.jsonl'
    records=[record(t,t if t<200 else t+500) for t in range(0,500,10)]
    source.write_text(''.join(json.dumps(r)+'\n' for r in records))
    report,_,prices=run(source,tmp_path/'out')
    assert report['event_count']==1
    assert len(prices['outcomes'])==3
    import csv
    with (tmp_path/'out'/'price_response.csv').open() as stream:
        exported=list(csv.DictReader(stream))
    assert len(exported)==6
    assert {r['horizon_seconds'] for r in exported}=={'30','60','120'}


def test_unfinished_event_is_explicitly_censored():
    assert cluster([interval(10),interval(20,1,10)])[0]['right_censored']
    assert not cluster([interval(10),interval(50,1,10)])[0]['right_censored']
