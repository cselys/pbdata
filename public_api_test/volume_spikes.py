"""Offline cumulative-volume spike candidates. No collector or broker imports."""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

METHOD = 'prior rolling time-weighted mean: sum(volume_delta)/sum(elapsed_seconds)'
ET = ZoneInfo('America/New_York')


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def volume(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class VolumeEngine:
    """Bounded incremental form of the Phase 2A detector, shared by offline/live."""
    def __init__(self, *, threshold=3.0, window=30, min_samples=10, max_age=60.0, max_gap=30.0, state=None):
        if not 1 <= min_samples <= window:
            raise ValueError('Require 1 <= min_samples <= window')
        if any(not math.isfinite(x) or x <= 0 for x in (threshold, max_age, max_gap)):
            raise ValueError('Threshold, max_age and max_gap must be finite and positive')
        self.config = dict(threshold=threshold, window=window, min_samples=min_samples, max_age=max_age, max_gap=max_gap)
        self.previous, self.history, self.counts, self.per_symbol = {}, {}, Counter(), {}
        self.line_number = 0
        if state:
            self.line_number = state['line_number']
            self.counts = Counter(state['counts'])
            self.per_symbol = {s:Counter(c) for s,c in state['per_symbol'].items()}
            self.history = {s:deque(v,maxlen=window) for s,v in state['history'].items()}
            for symbol, prior in state['previous'].items():
                received = timestamp(prior[0])
                self.previous[symbol] = (received,prior[1],(received.astimezone(ET).date(),prior[2]))

    def state(self):
        return dict(line_number=self.line_number, counts=dict(self.counts),
            per_symbol={s:dict(c) for s,c in self.per_symbol.items()},
            history={s:list(v) for s,v in self.history.items()},
            previous={s:[p[0].isoformat(),p[1],p[2][1]] for s,p in self.previous.items()})

    def process(self, record, expected_symbols=None):
        self.line_number += 1
        line_number = self.line_number
        threshold, window, min_samples, max_age, max_gap = (self.config[k] for k in ('threshold','window','min_samples','max_age','max_gap'))
        previous, history, counts, per_symbol = self.previous, self.history, self.counts, self.per_symbol
        intervals = []
        rows = record.get('observations')
        if not isinstance(rows, list):
            raise ValueError(f'Line {line_number}: observations must be an array')
        seen = set()
        for row in rows:
            symbol = row.get('symbol')
            if not isinstance(symbol, str) or not symbol:
                raise ValueError(f'Line {line_number}: missing symbol')
            if symbol in seen:
                raise ValueError(f'Line {line_number}: duplicate symbol {symbol}')
            seen.add(symbol)
            pc = per_symbol.setdefault(symbol, Counter())
            pc['observations'] += 1
            counts['observations'] += 1
            baseline = history.setdefault(symbol, deque(maxlen=window))
            received = timestamp(row.get('response_received_at_utc'))
            provider = timestamp(row.get('last_timestamp_raw'))
            current_volume = volume(row.get('volume'))
            age = (received-provider).total_seconds() if received and provider else None
            session = row.get('market_session')
            reason = None
            if row.get('category') != 'success' or not isinstance(row.get('http_status'), int) or not 200 <= row['http_status'] < 300:
                reason = 'failed_observation'
            elif received is None:
                reason = 'invalid_receive_time'
            elif current_volume is None:
                reason = 'invalid_volume'
            elif provider is None:
                reason = 'missing_or_invalid_last_timestamp'
            elif age < 0:
                reason = 'future_last_timestamp'
            elif age > max_age:
                reason = 'stale_last_timestamp'
            elif session not in ('premarket', 'regular', 'afterhours'):
                reason = 'unknown_or_closed_session'
            prior = previous.get(symbol)
            elapsed = (received-prior[0]).total_seconds() if received and prior else None
            key = (received.astimezone(ET).date(), session) if received else None
            item = dict(symbol=symbol, input_line=line_number,
                        received_at_utc=row.get('response_received_at_utc'),
                        last_timestamp_raw=row.get('last_timestamp_raw'),
                        last_age_seconds=age, market_session=session,
                        cumulative_volume=current_volume, elapsed_seconds=elapsed,
                        previous_received_at_utc=prior[0].isoformat() if prior else None,
                        previous_cumulative_volume=prior[1] if prior else None,
                        volume_delta=None, volume_rate=None, baseline_rate=None,
                        baseline_method=METHOD, baseline_sample_count=len(baseline), rvol=None)
            if reason:
                previous.pop(symbol, None)
                baseline.clear()
            else:
                if prior is None:
                    reason = 'first_valid_observation'
                elif key != prior[2]:
                    reason = 'session_boundary'
                elif elapsed <= 0:
                    reason = 'non_increasing_receive_time'
                elif elapsed > max_gap:
                    reason = 'gap_exceeds_limit'
                elif current_volume < prior[1]:
                    reason = 'negative_volume_correction_or_reset'
                if reason:
                    baseline.clear()
                else:
                    delta = current_volume-prior[1]
                    rate = delta/elapsed
                    base = sum(d for d, t in baseline)/sum(t for d, t in baseline) if baseline else None
                    item.update(volume_delta=delta, volume_rate=rate, baseline_rate=base,
                                baseline_sample_count=len(baseline))
                    pc['valid_intervals'] += 1
                    counts['valid_intervals'] += 1
                    if len(baseline) < min_samples:
                        reason = 'baseline_warmup'
                    elif base == 0:
                        reason = 'zero_baseline'
                    else:
                        item['rvol'] = rate/base
                        reason = 'spike_candidate' if item['rvol'] >= threshold else 'below_threshold'
                    baseline.append((delta, elapsed))
                previous[symbol] = (received, current_volume, key)
            item['status'] = reason
            pc[reason] += 1
            counts[reason] += 1
            intervals.append(item)
            if reason == 'spike_candidate':
                pass
        # A missing row must not silently bridge two successful samples.
        for symbol in (set(per_symbol) if expected_symbols is None else set(expected_symbols))-seen:
            previous.pop(symbol, None)
            history.setdefault(symbol, deque(maxlen=window)).clear()
            counts['missing_observation'] += 1
            per_symbol.setdefault(symbol, Counter())['missing_observation'] += 1
        return intervals


def detect(records, *, threshold=3.0, window=30, min_samples=10, max_age=60.0, max_gap=30.0):
    """Batch adapter around the shared bounded incremental detector."""
    engine = VolumeEngine(threshold=threshold, window=window, min_samples=min_samples, max_age=max_age, max_gap=max_gap)
    intervals = []
    for record in records:
        intervals.extend(engine.process(record))
    sweeps, counts, per_symbol = engine.line_number, engine.counts, engine.per_symbol
    candidates = [dict(row) for row in intervals if row['status'] == 'spike_candidate']
    quality_reasons = ('failed_observation', 'invalid_receive_time', 'invalid_volume',
        'missing_or_invalid_last_timestamp', 'future_last_timestamp', 'stale_last_timestamp',
        'unknown_or_closed_session', 'session_boundary', 'non_increasing_receive_time',
        'gap_exceeds_limit', 'negative_volume_correction_or_reset', 'missing_observation')
    return dict(config=dict(threshold=threshold, window=window, min_samples=min_samples,
                max_age_seconds=max_age, max_gap_seconds=max_gap, baseline_method=METHOD),
        summary=dict(sweeps=sweeps, symbols=len(per_symbol), counts=dict(counts),
            detections_by_symbol={s:c['spike_candidate'] for s,c in sorted(per_symbol.items())},
            data_quality_exclusions={k:counts[k] for k in quality_reasons},
            per_symbol={s:dict(c) for s,c in sorted(per_symbol.items())}),
        candidates=candidates, intervals=intervals,
        limitations=['Last-trade timestamp is a freshness proxy, not a volume-update timestamp.',
            'Receive-time rates can reflect delayed or batched provider updates.',
            'Candidates are descriptive anomalies, not trading signals or predictions.',
            'Session labels are inherited from the collector; invalid samples restart baseline.'])


def run(input_path, output_dir, **options):
    source = Path(input_path)
    directory = Path(output_dir)
    paths = [directory / (source.stem + suffix) for suffix in
             ('_volume_spikes.json', '_volume_spikes.csv')]
    if any(p.resolve() == source.resolve() for p in paths):
        raise ValueError('Output must not overwrite input')
    digest = hashlib.sha256()
    def records():
        with source.open('rb') as stream:
            for line in stream:
                digest.update(line)
                yield json.loads(line)
    report = detect(records(), **options)
    report['input'] = dict(path=str(source.resolve()), sha256=digest.hexdigest())
    directory.mkdir(parents=True, exist_ok=True)
    paths[0].write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    fields = ['symbol', 'rvol', 'volume_rate', 'baseline_rate', 'baseline_method',
              'baseline_sample_count', 'elapsed_seconds', 'volume_delta', 'cumulative_volume',
              'received_at_utc', 'previous_received_at_utc', 'previous_cumulative_volume', 'last_timestamp_raw', 'last_age_seconds', 'market_session', 'input_line', 'status']
    with paths[1].open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(report['candidates'], key=lambda c:c['rvol'], reverse=True))
    return report, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output-dir', default=Path('results/volume_spikes'), type=Path)
    parser.add_argument('--threshold', type=float, default=3.0)
    parser.add_argument('--window', type=int, default=30, help='Prior valid intervals in baseline')
    parser.add_argument('--min-samples', type=int, default=10)
    parser.add_argument('--max-age', type=float, default=60, help='Maximum last-trade age in seconds')
    parser.add_argument('--max-gap', type=float, default=30, help='Maximum receive-time interval in seconds')
    args = vars(parser.parse_args())
    source, destination = args.pop('input'), args.pop('output_dir')
    report, paths = run(source, destination, **args)
    print(json.dumps(report['summary'], indent=2))
    for path in paths:
        print(path.resolve())


if __name__ == '__main__':
    main()
