"""Phase 2B offline event and price-response validation; no broker imports."""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import mean, median

from public_api_test.volume_spikes import ET, detect, timestamp, volume

HORIZONS = (30, 60, 120)


def moment(row):
    return timestamp(row['received_at_utc'])


def session_key(row):
    return (moment(row).astimezone(ET).date(), row['market_session'])


def by_symbol(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['symbol']].append(row)
    return grouped


class EventEngine:
    """Shared causal clustering/cooldown state; closed events are emitted, not retained."""
    def __init__(self, threshold=3.0, event_gap=30.0, cooldown=120.0, *, state=None, track_lines=False):
        if not math.isfinite(threshold) or threshold <= 0 or any(not math.isfinite(x) or x < 0 for x in (event_gap,cooldown)):
            raise ValueError('Invalid event configuration')
        self.threshold, self.event_gap, self.cooldown = threshold,event_gap,cooldown
        self.track_lines = track_lines
        self.symbols = state or {}

    def close(self, symbol, at, reason):
        state = self.symbols.get(symbol)
        if not state or not state.get('active'):
            return []
        event = dict(state['active'])
        state['active'],state['pending_volume'] = None,0
        return [dict(transition='close', at=at, reason=reason, event=event)]

    def process(self, row):
        symbol = row['symbol']
        state = self.symbols.setdefault(symbol,dict(active=None,pending_volume=0,last_alert=None,last_session=None))
        changes = []
        if row['volume_delta'] is None:
            return self.close(symbol,row['received_at_utc'],'quality_break')
        key = [moment(row).astimezone(ET).date().isoformat(),row['market_session']]
        if key != state['last_session']:
            changes.extend(self.close(symbol,row['received_at_utc'],'session_boundary'))
            state['last_alert'],state['last_session'] = None,key
        t = moment(row)
        active = state['active']
        if active and (t-timestamp(active['end'])).total_seconds() > self.event_gap:
            changes.extend(self.close(symbol,row['received_at_utc'],'quiet_gap'))
        active = state['active']
        if active:
            state['pending_volume'] += row['volume_delta']
        if row['rvol'] is None or row['rvol'] < self.threshold:
            return changes
        if active is None:
            alert = state['last_alert'] is None or (t-timestamp(state['last_alert'])).total_seconds() >= self.cooldown
            active = dict(event_id=f'{symbol}:{row["input_line"]}', symbol=symbol,
                decision_time=row['received_at_utc'], decision_input_line=row['input_line'],
                start=row['received_at_utc'], end=row['received_at_utc'],
                volume_interval_start=row['previous_received_at_utc'],market_session=row['market_session'],
                peak_rvol=row['rvol'],decision_rvol=row['rvol'],decision_baseline_rate=row['baseline_rate'],
                decision_baseline_sample_count=row['baseline_sample_count'],baseline_method=row['baseline_method'],
                total_volume_delta=row['volume_delta'],candidate_volume_delta=row['volume_delta'],candidate_count=1,
                duration_seconds=0.0,volume_span_seconds=row['elapsed_seconds'],alert_eligible=alert,
                cooldown_suppressed=not alert,right_censored=False)
            if self.track_lines:
                active['candidate_lines'] = [row['input_line']]
            state['active'] = active
            if alert:
                state['last_alert'] = t.isoformat()
            transition = 'open'
        else:
            active['end'] = row['received_at_utc']
            active['peak_rvol'] = max(active['peak_rvol'],row['rvol'])
            active['total_volume_delta'] += state['pending_volume']
            active['candidate_volume_delta'] += row['volume_delta']
            active['candidate_count'] += 1
            if self.track_lines:
                active['candidate_lines'].append(row['input_line'])
            active['duration_seconds'] = (t-timestamp(active['start'])).total_seconds()
            active['volume_span_seconds'] = (t-timestamp(active['volume_interval_start'])).total_seconds()
            transition = 'update'
        state['pending_volume'] = 0
        # Copy optional list too: offline consumers keep prior transition snapshots.
        event = dict(active)
        if self.track_lines:
            event['candidate_lines'] = list(active['candidate_lines'])
        changes.append(dict(transition=transition,at=row['received_at_utc'],reason='candidate',event=event))
        return changes


def cluster(intervals, threshold=3.0, event_gap=30.0, cooldown=120.0):
    """Backward-compatible offline adapter for the shared causal event engine."""
    engine = EventEngine(threshold,event_gap,cooldown,track_lines=True)
    events = {}
    for symbol, rows in sorted(by_symbol(intervals).items()):
        for row in rows:
            for change in engine.process(row):
                event=change['event']
                events[event['event_id']]=event
    for state in engine.symbols.values():
        if state['active']:
            event=dict(state['active'],right_censored=True)
            events[event['event_id']]=event
    return sorted(events.values(),key=lambda e:(timestamp(e['decision_time']),e['symbol']))


def price_rows(records, max_age):
    result = defaultdict(list)
    for line, record in enumerate(records, 1):
        for row in record['observations']:
            received = timestamp(row.get('response_received_at_utc'))
            provider = timestamp(row.get('last_timestamp_raw'))
            price = volume(row.get('price'))
            age = (received-provider).total_seconds() if received and provider else None
            status = row.get('http_status')
            valid = (row.get('category') == 'success' and isinstance(status, int)
                and 200 <= status < 300 and price is not None and price > 0
                and age is not None and 0 <= age <= max_age
                and row.get('market_session') in ('premarket', 'regular', 'afterhours'))
            result[row['symbol']].append(dict(input_line=line, received_at_utc=row.get('response_received_at_utc'),
                last_timestamp_raw=row.get('last_timestamp_raw'), price=price, valid=valid,
                market_session=row.get('market_session')))
    return result


def response(rows, anchor_line, horizon, *, tolerance=15.0, max_gap=30.0, next_event=None):
    """First recorded sample at/after target, with bounded overshoot; no interpolation."""
    result = dict(horizon_seconds=horizon, status=None, signed_return_pct=None,
        absolute_return_pct=None, max_up_return_pct=None, max_down_return_pct=None)
    index = next(i for i,r in enumerate(rows) if r['input_line'] == anchor_line)
    anchor = rows[index]
    result.update(anchor_time=anchor['received_at_utc'], anchor_price=anchor['price'],
                  anchor_last_timestamp_raw=anchor['last_timestamp_raw'])
    def missing(reason):
        result['status'] = reason
        return result
    if not anchor['valid']:
        return missing('invalid_anchor_price')
    start = moment(anchor)
    target = start + timedelta(seconds=horizon)
    result['target_time'] = target.isoformat()
    endpoint_index = next((i for i in range(index+1, len(rows))
        if moment(rows[i]) is not None and moment(rows[i]) >= target), None)
    if endpoint_index is None:
        return missing('recording_ended_before_horizon')
    endpoint = rows[endpoint_index]
    end = moment(endpoint)
    result.update(endpoint_time=endpoint['received_at_utc'], endpoint_price=endpoint['price'],
        endpoint_last_timestamp_raw=endpoint['last_timestamp_raw'],
        actual_elapsed_seconds=(end-start).total_seconds(), overshoot_seconds=(end-target).total_seconds())
    if end > target+timedelta(seconds=tolerance):
        return missing('horizon_tolerance_exceeded')
    if next_event is not None and next_event <= end:
        return missing('overlapping_separate_event')
    path = rows[index:endpoint_index+1]
    if any(not r['valid'] for r in path):
        return missing('invalid_price_in_path')
    if any(session_key(r) != session_key(anchor) for r in path):
        return missing('session_boundary')
    if any(not 0 < (moment(b)-moment(a)).total_seconds() <= max_gap for a,b in zip(path,path[1:])):
        return missing('price_path_gap_or_non_increasing_time')
    if timestamp(endpoint['last_timestamp_raw']) <= start:
        return missing('no_post_decision_trade_timestamp')
    returns = [(r['price']/anchor['price']-1)*100 for r in path]
    result.update(status='available', signed_return_pct=returns[-1], absolute_return_pct=abs(returns[-1]),
        max_up_return_pct=max(returns), max_down_return_pct=min(returns), sampled_price_count=len(path))
    return result


def describe(values):
    return dict(count=len(values), mean=mean(values) if values else None,
                median=median(values) if values else None)


def analyze_prices(records, intervals, events, *, max_age=60, max_gap=30,
                   tolerance=15, match_lookback=600, match_rate_factor=2):
    prices = price_rows(records, max_age)
    interval_groups = by_symbol(intervals)
    event_groups = by_symbol(events)
    outcomes, matches, used = [], [], defaultdict(list)
    for symbol, group in event_groups.items():
        candidates = [r for r in interval_groups[symbol] if r['status'] == 'spike_candidate']
        for ei, event in enumerate(group):
            decision = timestamp(event['decision_time'])
            next_event = timestamp(group[ei+1]['decision_time']) if ei+1 < len(group) else None
            # Prior-only matching: all control follow-up and contamination checks are
            # completed before this event decision. No future event information is used.
            eligible = []
            for row in interval_groups[symbol]:
                if row['rvol'] is None or row['status'] != 'below_threshold':
                    continue
                t = moment(row)
                if not (120+tolerance <= (decision-t).total_seconds() <= match_lookback):
                    continue
                if session_key(row) != (decision.astimezone(ET).date(), event['market_session']):
                    continue
                ratio = row['baseline_rate']/event['decision_baseline_rate']
                if not 1/match_rate_factor <= ratio <= match_rate_factor:
                    continue
                finish = t+timedelta(seconds=120+tolerance)
                if any(t-timedelta(seconds=120) <= moment(c) <= finish for c in candidates):
                    continue
                if any(t <= b and finish >= a for a,b in used[symbol]):
                    continue
                # Anchor quality is known at event decision. Follow-up quality is
                # NOT used to choose a control: missing outcomes remain visible.
                anchor = next(p for p in prices[symbol] if p['input_line'] == row['input_line'])
                if not anchor['valid']:
                    continue
                eligible.append(row)
            control = max(eligible, key=moment) if eligible else None
            match = dict(event_id=event['event_id'], symbol=symbol,
                status='matched' if control else 'no_prior_eligible_control',
                eligible_control_count=len(eligible), control_time=control['received_at_utc'] if control else None,
                control_input_line=control['input_line'] if control else None,
                control_baseline_rate=control['baseline_rate'] if control else None)
            matches.append(match)
            if control:
                used[symbol].append((moment(control),moment(control)+timedelta(seconds=120+tolerance)))
            for h in HORIZONS:
                event_result = response(prices[symbol], event['decision_input_line'], h,
                    tolerance=tolerance, max_gap=max_gap, next_event=next_event)
                control_result = response(prices[symbol], control['input_line'], h,
                    tolerance=tolerance, max_gap=max_gap) if control else dict(status='no_matched_control')
                outcomes.append(dict(event_id=event['event_id'], symbol=symbol, horizon_seconds=h,
                    event=event_result, control=control_result))
    summary = {}
    for h in HORIZONS:
        rows = [r for r in outcomes if r['horizon_seconds'] == h]
        available = [r['event'] for r in rows if r['event']['status'] == 'available']
        paired = [r for r in rows if r['event']['status'] == r['control']['status'] == 'available']
        summary[str(h)] = dict(event_status_counts=dict(Counter(r['event']['status'] for r in rows)),
            control_status_counts=dict(Counter(r['control']['status'] for r in rows)),
            event_signed_return_pct=describe([r['signed_return_pct'] for r in available]),
            event_absolute_return_pct=describe([r['absolute_return_pct'] for r in available]),
            paired_count=len(paired),
            paired_event_signed_return_pct=describe([r['event']['signed_return_pct'] for r in paired]),
            paired_control_signed_return_pct=describe([r['control']['signed_return_pct'] for r in paired]),
            paired_event_absolute_return_pct=describe([r['event']['absolute_return_pct'] for r in paired]),
            paired_control_absolute_return_pct=describe([r['control']['absolute_return_pct'] for r in paired]),
            paired_absolute_difference_pct=describe([r['event']['absolute_return_pct']-r['control']['absolute_return_pct'] for r in paired]))
    return dict(matches=matches, outcomes=outcomes, summary=summary,
                matching_counts=dict(Counter(m['status'] for m in matches)))


def quality_diagnostics(intervals):
    suspected, large, zero_count, zero_then_positive = [], [], 0, 0
    for symbol, rows in by_symbol(intervals).items():
        zero_run, zero_duration, prior = 0, 0, None
        for row in rows:
            delta = row['volume_delta']
            if delta is None:
                zero_run, zero_duration, prior = 0, 0, None
                continue
            if delta == 0:
                zero_run += 1
                zero_duration += row['elapsed_seconds']
                zero_count += 1
            else:
                zero_then_positive += bool(zero_run)
                if row['rvol'] is not None and row['rvol'] >= 3:
                    diagnostic = dict(symbol=symbol, input_line=row['input_line'],
                        received_at_utc=row['received_at_utc'], volume_delta=delta, rvol=row['rvol'],
                        prior_zero_intervals=zero_run, prior_zero_duration_seconds=zero_duration,
                        last_age_seconds=row['last_age_seconds'],
                        prior_last_age_seconds=prior['last_age_seconds'] if prior else None,
                        last_timestamp_raw=row['last_timestamp_raw'],
                        prior_last_timestamp_raw=prior['last_timestamp_raw'] if prior else None,
                        unchanged_last_trade_timestamp=bool(prior and timestamp(prior['last_timestamp_raw']) == timestamp(row['last_timestamp_raw'])),
                        refreshed_after_aged_trade=bool(prior and prior['last_age_seconds'] >= 30 and row['last_age_seconds'] < prior['last_age_seconds']),
                        interpretation='Unverified anomaly; no dedicated volume-update timestamp')
                    large.append(diagnostic)
                    if zero_run:
                        diagnostic = dict(diagnostic, interpretation='Suspected delayed reporting OR genuine burst after inactivity; unconfirmed')
                        suspected.append(diagnostic)
                zero_run, zero_duration = 0, 0
            prior = row
    return dict(zero_delta_intervals=zero_count, zero_runs_followed_by_positive_delta=zero_then_positive,
        zero_then_large_delta=suspected, suspected_delayed_reporting_count=len(suspected),
        confirmed_anomalies=0,
        large_deltas_with_unchanged_last_trade_timestamp=sum(r['unchanged_last_trade_timestamp'] for r in large),
        large_deltas_after_aged_trade_refresh=sum(r['refreshed_after_aged_trade'] for r in large),
        largest_candidate_deltas=sorted(large,key=lambda r:r['volume_delta'],reverse=True)[:20])


def sensitivity(records, *, min_samples, max_age, max_gap, event_gap, cooldown):
    runs, reports = {}, []
    for window in (30,60,90):
        for warmup in ('fixed_minimum', 'full_window'):
            minimum = min_samples if warmup == 'fixed_minimum' else window
            result = detect(records, window=window, min_samples=minimum, max_age=max_age, max_gap=max_gap)
            runs[(window,warmup)] = result
    common = set.intersection(*[{(r['symbol'],r['input_line']) for r in v['intervals'] if r['rvol'] is not None} for v in runs.values()])
    for (window,warmup), result in runs.items():
        eligible = [r for r in result['intervals'] if r['rvol'] is not None]
        first = {}
        for row in eligible:
            first.setdefault(row['symbol'],row['received_at_utc'])
        for threshold in (2.0,3.0,4.0,5.0):
            candidates = [r for r in eligible if r['rvol'] >= threshold]
            events = cluster(result['intervals'],threshold,event_gap,cooldown)
            cc, ec = Counter(r['symbol'] for r in candidates),Counter(e['symbol'] for e in events)
            reports.append(dict(window=window,warmup_policy=warmup,min_samples=result['config']['min_samples'],
                threshold=threshold, eligible_observations=len(eligible),
                warmup_intervals=result['summary']['counts'].get('baseline_warmup',0),
                warmup_symbol_seconds=sum(r['elapsed_seconds'] for r in result['intervals'] if r['status']=='baseline_warmup'),
                first_eligible_time_by_symbol=first, common_eligible_observations=len(common),
                common_eligible_candidates=sum((r['symbol'],r['input_line']) in common for r in candidates),
                candidate_count=len(candidates),event_count=len(events),
                alert_eligible_events=sum(e['alert_eligible'] for e in events),
                symbols_with_events=len(ec),
                per_symbol={s:dict(candidates=cc[s],events=ec[s]) for s in sorted(result['summary']['per_symbol'])},
                quality_exclusions=result['summary']['data_quality_exclusions']))
    return reports, runs[(30,'fixed_minimum')]


def save_csv(path, rows, fields):
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def run(source, output_dir, *, event_gap=30, cooldown=120, min_samples=10,
        max_age=60, max_gap=30, horizon_tolerance=15, match_lookback=600, match_rate_factor=2):
    if not 1 <= min_samples <= 30:
        raise ValueError('min_samples must be 1..30 for the 30/60/90 comparison')
    values = (event_gap,cooldown,max_age,max_gap,horizon_tolerance,match_lookback,match_rate_factor)
    if any(not math.isfinite(v) or v < 0 for v in values) or min(max_age,max_gap,match_lookback) <= 0 or match_rate_factor < 1:
        raise ValueError('Invalid timing, quality or matching configuration')
    source, output_dir = Path(source), Path(output_dir)
    data = source.read_bytes()
    records = [json.loads(line) for line in data.splitlines()]
    sensitivity_report, base = sensitivity(records,min_samples=min_samples,max_age=max_age,max_gap=max_gap,event_gap=event_gap,cooldown=cooldown)
    events = cluster(base['intervals'],3,event_gap,cooldown)
    prices = analyze_prices(records,base['intervals'],events,max_age=max_age,max_gap=max_gap,
        tolerance=horizon_tolerance,match_lookback=match_lookback,match_rate_factor=match_rate_factor)
    diagnostics = quality_diagnostics(base['intervals'])
    distribution = {s:dict(candidates=base['summary']['detections_by_symbol'][s],
        events=sum(e['symbol']==s for e in events),alert_eligible_events=sum(e['symbol']==s and e['alert_eligible'] for e in events))
        for s in sorted(base['summary']['per_symbol'])}
    config = dict(event_gap_seconds=event_gap,cooldown_seconds=cooldown,min_samples=min_samples,
        max_age_seconds=max_age,max_gap_seconds=max_gap,horizon_tolerance_seconds=horizon_tolerance,
        match_lookback_seconds=match_lookback,match_rate_factor=match_rate_factor,
        primary_threshold=3,primary_window=30)
    report = dict(input=dict(path=str(source.resolve()),sha256=hashlib.sha256(data).hexdigest()),
        config=config,phase2a_summary=base['summary'],event_count=len(events),
        alert_eligible_events=sum(e['alert_eligible'] for e in events),events=events,
        per_symbol=distribution,quality_diagnostics=diagnostics)
    files = {name:output_dir/name for name in ('events.json','events.csv','sensitivity.json','sensitivity.csv',
        'price_response.json','price_response.csv','per_symbol.csv','PHASE_2B_REPORT.md')}
    if source.resolve() in [p.resolve() for p in files.values()]:
        raise ValueError('Output must not overwrite input')
    output_dir.mkdir(parents=True,exist_ok=True)
    for name,value in [('events.json',report),('sensitivity.json',dict(config=config,comparisons=sensitivity_report)),('price_response.json',dict(config=config,**prices))]:
        files[name].write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')
    save_csv(files['events.csv'],events,['event_id','symbol','decision_time','start','end','peak_rvol',
        'decision_rvol','decision_baseline_rate','decision_baseline_sample_count','baseline_method',
        'total_volume_delta','candidate_volume_delta','duration_seconds','volume_span_seconds','candidate_count',
        'alert_eligible','cooldown_suppressed','right_censored'])
    save_csv(files['sensitivity.csv'],sensitivity_report,['window','warmup_policy','min_samples','threshold',
        'eligible_observations','warmup_intervals','warmup_symbol_seconds','common_eligible_observations',
        'common_eligible_candidates','candidate_count','event_count','alert_eligible_events','symbols_with_events'])
    save_csv(files['per_symbol.csv'],[dict(symbol=s,**v) for s,v in distribution.items()],['symbol','candidates','events','alert_eligible_events'])
    flat = [dict(r[g],event_id=r['event_id'],symbol=r['symbol'],group=g,horizon_seconds=r['horizon_seconds'])
        for r in prices['outcomes'] for g in ('event','control')]
    save_csv(files['price_response.csv'],flat,['event_id','symbol','group','horizon_seconds','status','anchor_time',
        'anchor_price','anchor_last_timestamp_raw','target_time','endpoint_time','endpoint_price',
        'endpoint_last_timestamp_raw','actual_elapsed_seconds','overshoot_seconds','signed_return_pct',
        'absolute_return_pct','max_up_return_pct','max_down_return_pct','sampled_price_count'])
    files['PHASE_2B_REPORT.md'].write_text(render_report(report,sensitivity_report,prices),encoding='utf-8')
    return report, sensitivity_report, prices


def render_report(report, sensitivity_report, prices):
    q=report['quality_diagnostics']
    text = [ '# Phase 2B: offline volume spike quality validation', '',
        f"Input: `{report['input']['path']}`", f"SHA-256: `{report['input']['sha256']}`", '',
        '## Methods and interpretation', '',
        'Primary analysis: RVOL 3.0, prior-only time-weighted 30-interval baseline. '
        'Configuration: `'+json.dumps(report['config'])+'`.',
        'Decision time is receipt of the FIRST candidate, never the eventual peak or end. '
        'A cluster joins candidates whose receive times are within event-gap seconds; invalid volume intervals break clusters. '
        'Only one alert opportunity per cluster. New clusters within cooldown of the last eligible decision are retained but suppressed. '
        'Peak, end, count and aggregate volume are retrospective descriptors. Total volume includes valid non-candidate intervals '
        'between the first and last candidate; candidate volume is separately recorded. Duration is last minus first candidate receipt; '
        'volume span includes the first candidate interval. An isolated event has zero receipt-span duration. '
        'Right-censored events could still extend beyond the final recorded observation; their reported end is the last observed candidate.',
        'Prices use the decision observation and the first recorded observation at/after each 30/60/120-second target, '
        'within the configured overshoot tolerance. No interpolation. Paths with invalid/stale prices, large gaps, session changes '
        'or a separate subsequent event are excluded; endpoint trade timestamp must be after decision receipt. '
        'Up/down excursions are sampled price extrema relative to the starting price (long-reference MFE/MAE, including zero), '
        'not intrainterval highs/lows or directional classifications. All events, including cooldown-suppressed ones, are analyzed.',
        'Controls: same symbol, same New York date/session, eligible non-candidate, baseline rate within configured factor, '
        'nearest earlier receipt within lookback, and 120 seconds plus tolerance of follow-up completed before event decision. '
        'No candidate from 120 seconds before control through its follow-up. Controls are not reused and their follow-up windows '
        'do not overlap per symbol. Matching uses no data after event decision and does not select on future return or outcome availability. '
        'Subsequent-event exclusions are outcome-quality flags only, not filters on event detection or control selection. '
        'Compare only paired available outcomes; coverage and unmatched cases are reported.',
        'Sensitivity uses identical Phase 2A quality rules. Fixed minimum isolates window length; full-window warmup shows '
        'sample eligibility costs. Common eligible candidate counts use the intersection of eligible symbol/line observations across '
        'all six baseline configurations (retrospective comparison only). Warmup seconds sum symbol intervals, not wall-clock runtime.', '',
        '## Actual results', '',
        f"Candidates: {report['phase2a_summary']['counts'].get('spike_candidate',0)}; events: {report['event_count']}; "
        f"eligible alert opportunities: {report['alert_eligible_events']}. These are descriptive counts, not trading signals.", '',
        '| Window | Warmup | Min | RVOL | Eligible | Candidates | Events | Alert eligible |',
        '|---:|---|---:|---:|---:|---:|---:|---:|']
    for r in sensitivity_report:
        text.append(f"| {r['window']} | {r['warmup_policy']} | {r['min_samples']} | {r['threshold']} | {r['eligible_observations']} | {r['candidate_count']} | {r['event_count']} | {r['alert_eligible_events']} |")
    text += ['', 'Baseline warmup and eligibility (at RVOL 3):', '',
        '| Window | Policy | Warmup intervals | Eligible intervals | Common eligible candidates |',
        '|---:|---|---:|---:|---:|']
    text += [f"| {r['window']} | {r['warmup_policy']} | {r['warmup_intervals']} | {r['eligible_observations']} | {r['common_eligible_candidates']} |"
        for r in sensitivity_report if r['threshold']==3]
    text += [f"Common eligible denominator across all six baseline configurations: {sensitivity_report[0]['common_eligible_observations']}."]
    text += ['', '## Price response', '', 'Matching: `'+json.dumps(prices['matching_counts'])+'`.', '',
        '| Horizon | Available events | Paired | Paired mean absolute event return % | Paired mean absolute control return % |',
        '|---:|---:|---:|---:|---:|']
    for h,r in prices['summary'].items():
        text.append(f"| {h}s | {r['event_absolute_return_pct']['count']} | {r['paired_count']} | {r['paired_event_absolute_return_pct']['mean']} | {r['paired_control_absolute_return_pct']['mean']} |")
    text += ['', 'Missing event horizons:']
    text += [f"- {h}s: `{json.dumps(r['event_status_counts'])}`" for h,r in prices['summary'].items()]
    text += ['', '## Data quality and readiness', '',
        f"Zero-delta intervals: {q['zero_delta_intervals']}; zero runs followed by positive volume: {q['zero_runs_followed_by_positive_delta']}; "
        f"zero-then-large (RVOL >=3) cases: {q['suspected_delayed_reporting_count']}. Details and the largest candidate deltas are in events.json.",
        f"Large deltas with unchanged last-trade timestamp: {q['large_deltas_with_unchanged_last_trade_timestamp']}; "
        f"large deltas after a last-trade age >=30 seconds refresh: {q['large_deltas_after_aged_trade_refresh']}. "
        'Neither measure timestamps volume updates. The 30-second diagnostic cutoff does not change detector eligibility.',
        'Zero-then-large cases: '+', '.join(f"{r['symbol']} at {r['received_at_utc']} ({r['rvol']:.2f}x, {r['volume_delta']:g} shares)" for r in q['zero_then_large_delta'])+'.',
        f"Single-candidate events: {sum(e['candidate_count']==1 for e in report['events'])} of {report['event_count']}. "
        'Clustering reduces duplicates but does not establish the economic meaning of the remaining events.',
        'These patterns are suspected delayed reporting OR real activity after inactivity, not confirmed feed delays or confirmed anomalies. '
        'There is no volume-update timestamp. Last-trade age cannot establish cumulative-volume freshness.',
        'Recommendation: suitable only for instrumented live shadow monitoring to collect further evidence; not ready for user-facing alerts. '
        'Retain raw observations, audit volume publication timing, and validate across multiple days/sessions and independent volume evidence '
        'before tuning alert thresholds. This single 30-minute regular-hours recording establishes neither predictive performance nor '
        'causal price impact. Repeated symbols, market-wide correlation, unmatched events, overlapping outcomes within extended clusters, '
        'and censoring prevent treating samples as independent. Earlier controls are not matched on market regime, price volatility or '
        'exact time of day; intraday changes can bias comparisons. No significance or predictive claim is made. '
        'The 30/60/90 windows are sample counts, not fixed durations; current data only partially fills longer windows after warmup.',
        'No API calls, training, orders or collector changes. Phase 2A is reused without modification. Raw observations are preserved.', '',
        '## Per-symbol event distribution', '', '| Symbol | Candidates | Events | Alert eligible |', '|---|---:|---:|---:|']
    text += [f"| {s} | {v['candidates']} | {v['events']} | {v['alert_eligible_events']} |" for s,v in report['per_symbol'].items()]
    return '\n'.join(text)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output-dir',type=Path,default=Path('results/phase2b'))
    for name,default in [('event-gap',30),('cooldown',120),('max-age',60),('max-gap',30),
                         ('horizon-tolerance',15),('match-lookback',600),('match-rate-factor',2)]:
        parser.add_argument('--'+name,type=float,default=default)
    parser.add_argument('--min-samples',type=int,default=10)
    args=vars(parser.parse_args())
    source=args.pop('input'); destination=args.pop('output_dir')
    report, _, prices=run(source,destination,**args)
    print(json.dumps(dict(events=report['event_count'],alert_eligible=report['alert_eligible_events'],
        matching=prices['matching_counts'],report=str((destination/'PHASE_2B_REPORT.md').resolve())),indent=2))


if __name__ == '__main__':
    main()
