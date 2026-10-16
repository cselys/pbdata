"""Bounded, sequential Batch polling. No live work occurs on import."""
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone

from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.sessions import annotate_quote, ET
from public_api_test.tests.test_freshness import stats, timestamp_statistics


def validate_symbols(symbols):
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not 1 <= len(symbols) <= 20:
        raise ValueError("batch_stability requires 1..20 symbols; larger lists are never split")
    if len(set(symbols)) != len(symbols):
        raise ValueError("batch_stability requires unique symbols")
    return symbols


def numeric_volume(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def cycle_observations(batch, symbols, received, previous, max_age=60, tolerance=0):
    request = next(iter(batch.get("results", [])), {})
    body = request.get("raw_response", {})
    quotes = body.get("quotes", []) if isinstance(body, dict) else []
    matching = {}
    for item in quotes if isinstance(quotes, list) else []:
        if isinstance(item, dict) and isinstance(item.get("instrument"), dict):
            matching.setdefault(item["instrument"].get("symbol"), item)
    outcomes = {i["symbol"]: i for i in batch.get("instrument_results", [])}
    http_ok = (200 <= (batch.get("http_status") or 0) < 300 and
        batch.get("category") in ("success", "partial_result", "empty_or_failed_quotes"))
    rows = []
    for symbol in symbols:
        raw = matching.get(symbol, {})
        parsed = outcomes.get(symbol, {})
        category = parsed.get("category", "missing_symbol") if http_ok else "request_failed"
        row = dict(symbol=symbol, category=category, price=parsed.get("price"),
            http_status=batch.get("http_status"), request_duration_ms=batch.get("http_latency_ms"),
            endpoint_url=request.get("endpoint_url"), response_received_at_utc=received.isoformat(),
            http_response_received_at_utc=request.get("response_received_at_utc"),
            receive_time_source="http_response" if request.get("response_received_at_utc") else "request_finished_no_response",
            raw_response=raw, volume=raw.get("volume"), bid=raw.get("bid"), ask=raw.get("ask"),
            last_timestamp_raw=raw.get("lastTimestamp"), bid_timestamp_raw=raw.get("bidTimestamp"),
            ask_timestamp_raw=raw.get("askTimestamp"), api_error_code=parsed.get("api_error_code"),
            api_error_message=parsed.get("api_error_message"))
        annotate_quote(row, max_age=max_age, clock_skew_tolerance=tolerance)
        prior = previous.get(symbol)
        volume = numeric_volume(row["volume"])
        old_volume = numeric_volume(prior.get("volume")) if prior else None
        if category != "success":
            reason = "failed_sample"
        elif prior is None:
            reason = "first_sample"
        elif prior["category"] != "success":
            reason = "previous_failed_sample"
        elif (prior["market_session"] != row["market_session"] or
              datetime.fromisoformat(prior["response_received_at_utc"]).astimezone(ET).date() != received.astimezone(ET).date()):
            reason = "session_boundary"
        elif volume is None or old_volume is None:
            reason = "missing_or_invalid_volume"
        elif volume < old_volume:
            reason = "cumulative_volume_reset"
        else:
            reason = "valid"
        row.update(volume_delta=volume-old_volume if reason == "valid" else None,
            volume_delta_status=reason)
        previous[symbol] = row
        rows.append(row)
    return rows


def monitor_batches(client, symbols, interval, duration, emit, *, clock=time.monotonic,
                    sleep=time.sleep, wall_clock=lambda: datetime.now(timezone.utc),
                    max_age=60, tolerance=0):
    symbols = validate_symbols(symbols)
    if interval <= 0 or duration <= 0 or not math.isfinite(interval) or not math.isfinite(duration):
        raise ValueError("Interval and duration must be finite and positive")
    if not client.token or not client.account_id:
        raise ValueError("Initialize authentication/account once before monitoring")
    start = clock()
    deadline = start + duration
    scheduled = start
    cycles, previous = [], {}
    last_start = None
    interrupted = False
    skipped_slots = 0
    try:
        while clock() < deadline:
            due = max(scheduled, client.next_request)
            sleep(max(0, min(due, deadline)-clock()))
            if clock() >= deadline:
                break
            sent = clock()
            sent_utc = wall_clock().astimezone(timezone.utc).isoformat()
            try:
                batch = client.get_quotes_batch(symbols)  # No chunking or immediate retry.
            except Exception as error:
                batch = dict(category="client_error", http_status=None, api_error_code=type(error).__name__,
                    quote_request_count=None, http_latency_ms=None, results=[], instrument_results=[],
                    returned_symbol_list=[], missing_symbols=list(symbols))
            finished = wall_clock().astimezone(timezone.utc)
            request = next(iter(batch.get("results", [])), {})
            receive_raw = request.get("response_received_at_utc")
            received = datetime.fromisoformat(receive_raw) if receive_raw else finished
            rows = cycle_observations(batch, symbols, received, previous, max_age, tolerance)
            record = dict(cycle=len(cycles)+1, scheduled_offset_seconds=scheduled-start,
                actual_start_offset_seconds=sent-start, scheduling_lateness_seconds=sent-scheduled,
                actual_polling_interval_seconds=sent-last_start if last_start is not None else None,
                request_start_utc=sent_utc, request_finished_utc=finished.isoformat(),
                response_received_at_utc=receive_raw, batch=batch, observations=rows)
            record = client._sanitize_batch(record)
            cycles.append(record)
            emit(record)
            last_start = sent
            scheduled += interval
            if scheduled < clock():
                new_slot = start + (math.floor((clock()-start)/interval)+1)*interval
                skipped_slots += round((new_slot-scheduled)/interval)
                scheduled = new_slot
    except KeyboardInterrupt:
        interrupted = True
    return cycles, interrupted, clock()-start, skipped_slots


def summarize_batches(cycles, symbols):
    batches = [c["batch"] for c in cycles]
    observations = [o for c in cycles for o in c["observations"]]
    http_success = sum(200 <= (b.get("http_status") or 0) < 300 for b in batches)
    def latency(values):
        result = stats(values)
        if result:
            result["p50"] = result["median"]
        return result
    def ages(rows):
        result = timestamp_statistics(rows)
        for side, diagnostic in result.items():
            diagnostic["negative_age_magnitude_seconds"] = stats([-o[f"{side}_age_seconds"] for o in rows
                if o["category"] == "success" and o.get(f"{side}_age_seconds") is not None and o[f"{side}_age_seconds"] < 0])
        return result
    return dict(total_cycles=len(cycles), successful_cycles=sum(b.get("category") == "success" for b in batches),
        quotes_http_requests=sum(b.get("quote_request_count") or 0 for b in batches),
        http_successful_cycles=http_success, http_success_rate=http_success/len(cycles) if cycles else None,
        request_failure_cycles=sum(b.get("category") not in ("success", "partial_result", "empty_or_failed_quotes") for b in batches),
        instrument_failures=sum(o["category"] not in ("success", "request_failed") for o in observations),
        http_status_distribution=dict(Counter(str(b.get("http_status")) for b in batches)),
        http_429_responses=sum(b.get("http_status") == 429 for b in batches),
        network_timeouts=sum(b.get("category") == "network_error" and "Timeout" in str(b.get("api_error_code")) for b in batches),
        network_errors=sum(b.get("category") == "network_error" and "Timeout" not in str(b.get("api_error_code")) for b in batches),
        client_errors=sum(b.get("category") == "client_error" for b in batches),
        http_latency_ms=latency([b["http_latency_ms"] for b in batches if b.get("http_latency_ms") is not None]),
        actual_polling_intervals_seconds=stats([c["actual_polling_interval_seconds"] for c in cycles if c["actual_polling_interval_seconds"] is not None]),
        timestamp_statistics=ages(observations),
        cycle_results=[dict(cycle=c["cycle"], http_status=c["batch"].get("http_status"),
            category=c["batch"].get("category"), returned_symbols=c["batch"].get("returned_symbol_list", []),
            missing_symbols=c["batch"].get("missing_symbols", [])) for c in cycles],
        per_symbol={symbol: dict(observations=sum(o["symbol"] == symbol for o in observations),
            timestamp_statistics=ages([o for o in observations if o["symbol"] == symbol]),
            volume_delta_statistics=stats([o["volume_delta"] for o in observations if o["symbol"] == symbol and o["volume_delta"] is not None]),
            volume_interval_status_counts=dict(Counter(o["volume_delta_status"] for o in observations if o["symbol"] == symbol)),
            volume_samples=[dict(cycle=c["cycle"], volume=o["volume"], delta=o["volume_delta"], status=o["volume_delta_status"])
                for c in cycles for o in c["observations"] if o["symbol"] == symbol]) for symbol in symbols})


def run_batch_stability(symbols, interval_seconds=10, duration_seconds=1800,
                        requests_per_minute=60, max_age=60, clock_skew_tolerance=0):
    symbols = validate_symbols(symbols)
    if interval_seconds <= 0 or duration_seconds <= 0:
        raise ValueError("Interval and duration must be positive")
    client = PublicComClient(requests_per_minute=requests_per_minute)
    name = "batch_stability_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = RAW_OBSERVATIONS_DIR / (name + ".jsonl")
    summary_path = SUMMARY_STATS_DIR / (name + ".json")
    try:
        setup = client._sanitize_batch(client.prepare())
        if not client.token or not client.account_id:
            summary_path.write_text(json.dumps(dict(run_status="setup_failed", setup=setup), indent=2))
            print("Batch stability setup failed; no polling started. See " + str(summary_path))
            return False
        with raw_path.open("w", encoding="utf-8") as stream:
            def emit(record):
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                batch = record["batch"]
                print(f"Cycle {record['cycle']}: HTTP={batch.get('http_status')} "
                    f"category={batch.get('category')} returned={len(batch.get('returned_symbol_list', []))}/{len(symbols)} "
                    f"latency_ms={batch.get('http_latency_ms')}", flush=True)
            cycles, interrupted, elapsed, skipped = monitor_batches(client, symbols,
                interval_seconds, duration_seconds, emit, max_age=max_age, tolerance=clock_skew_tolerance)
        summary = summarize_batches(cycles, symbols)
        summary.update(run_status="interrupted" if interrupted else "success" if cycles and summary["successful_cycles"] == len(cycles) else "degraded",
            symbols=symbols, interval_seconds=interval_seconds, duration_seconds=duration_seconds,
            elapsed_seconds=elapsed, skipped_schedule_slots=skipped, setup=setup,
            requests_per_minute=requests_per_minute, max_age_seconds=max_age,
            clock_skew_tolerance_seconds=clock_skew_tolerance)
        summary_path.write_text(json.dumps(client._sanitize_batch(summary), indent=2), encoding="utf-8")
        print(f"Raw observations: {raw_path}\nSummary: {summary_path}")
        return summary["run_status"] == "success"
    finally:
        client.close()
