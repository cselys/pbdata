import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from public_api_test.volume_spikes import detect, run

START = datetime(2026, 10, 12, 14, tzinfo=timezone.utc)


def sweep(t, v, **changes):
    stamp = (START + timedelta(seconds=t)).isoformat()
    row = dict(symbol='AAPL', category='success', http_status=200, volume=v,
               response_received_at_utc=stamp, last_timestamp_raw=stamp, market_session='regular')
    row.update(changes)
    return dict(observations=[row])


def evaluate(rows, **kwargs):
    return detect(rows, min_samples=2, window=3, **kwargs)


def test_prior_only_spike_and_normal():
    result = evaluate([sweep(0, 0), sweep(10, 100), sweep(20, 200), sweep(30, 600)])
    candidate, = result['candidates']
    assert candidate['baseline_rate'] == 10
    assert candidate['baseline_sample_count'] == 2
    assert candidate['rvol'] == 4
    assert candidate['elapsed_seconds'] == 10
    assert result['summary']['counts']['baseline_warmup'] == 2
    normal = evaluate([sweep(i*10, i*100) for i in range(8)])
    assert normal['candidates'] == []


def test_irregular_intervals_time_weighted():
    result = evaluate([sweep(0, 0), sweep(5, 100), sweep(25, 200), sweep(30, 400)])
    candidate, = result['candidates']
    assert candidate['baseline_rate'] == 8
    assert candidate['volume_rate'] == 40
    assert candidate['rvol'] == 5


def test_zero_volume_not_missing_and_zero_baseline():
    result = evaluate([sweep(i*10, 100) for i in range(5)])
    assert result['summary']['counts']['valid_intervals'] == 4
    assert result['summary']['counts']['zero_baseline'] == 2
    assert result['intervals'][-1]['volume_delta'] == 0
    assert result['intervals'][-1]['rvol'] is None
    assert result['candidates'] == []


@pytest.mark.parametrize('change,reason', [
    ({'volume': None}, 'invalid_volume'),
    ({'volume': -1}, 'invalid_volume'),
    ({'volume': 'NaN'}, 'invalid_volume'),
    ({'category': 'request_failed', 'http_status': 429}, 'failed_observation'),
    ({'last_timestamp_raw': None}, 'missing_or_invalid_last_timestamp'),
    ({'last_timestamp_raw': (START-timedelta(seconds=100)).isoformat()}, 'stale_last_timestamp'),
    ({'last_timestamp_raw': (START+timedelta(seconds=100)).isoformat()}, 'future_last_timestamp'),
])
def test_bad_samples_break_chain(change, reason):
    result = evaluate([sweep(0, 0), sweep(10, 100), sweep(20, 200, **change), sweep(30, 900)])
    assert result['intervals'][2]['status'] == reason
    assert result['intervals'][2]['volume_delta'] is None
    assert result['intervals'][3]['status'] == 'first_valid_observation'
    assert result['candidates'] == []


@pytest.mark.parametrize('row,reason', [
    (sweep(20, 50), 'negative_volume_correction_or_reset'),
    (sweep(20, 200, market_session='afterhours'), 'session_boundary'),
    (sweep(60, 200), 'gap_exceeds_limit'),
    (sweep(10, 200), 'non_increasing_receive_time'),
    (sweep(86420, 200), 'session_boundary'),
])
def test_interval_discontinuities(row, reason):
    result = evaluate([sweep(0, 0), sweep(10, 100), row])
    assert result['intervals'][-1]['status'] == reason
    assert result['intervals'][-1]['volume_delta'] is None
    assert result['candidates'] == []


def test_missing_observation_clears_baseline():
    result = evaluate([sweep(0, 0), sweep(10, 100), {'observations': []}, sweep(30, 900)])
    assert result['summary']['data_quality_exclusions']['missing_observation'] == 1
    assert result['intervals'][-1]['status'] == 'first_valid_observation'


def test_rolling_window():
    result = detect([sweep(i*10, v) for i,v in enumerate([0, 1000, 1100, 1200, 1500])],
                    window=2, min_samples=2)
    assert result['candidates'][-1]['baseline_rate'] == 10
    assert result['candidates'][-1]['rvol'] == 3


def test_reports_preserve_input_and_do_not_copy_sensitive_fields(tmp_path):
    source = tmp_path / 'input.jsonl'
    rows = [sweep(i*10, v) for i,v in enumerate([0, 100, 200, 600, 3000])]
    rows[0]['observations'][0]['raw_response'] = {'accessToken': 'SECRET', 'accountId': 'PRIVATE'}
    source.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    before = source.read_bytes()
    report, paths = run(source, tmp_path, window=3, min_samples=2)
    assert source.read_bytes() == before
    assert report['input']['sha256'] == hashlib.sha256(before).hexdigest()
    assert len(report['candidates']) == 2
    for path in paths:
        assert 'SECRET' not in path.read_text()
        assert 'PRIVATE' not in path.read_text()
    import csv
    csv_rows = list(csv.DictReader(paths[1].open()))
    assert float(csv_rows[0]['rvol']) >= float(csv_rows[1]['rvol'])


def test_invalid_configuration():
    with pytest.raises(ValueError):
        detect([], min_samples=31, window=30)
    with pytest.raises(ValueError):
        detect([], threshold=float('nan'))
