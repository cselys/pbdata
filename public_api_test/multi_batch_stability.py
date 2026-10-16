"""Full-universe multi-batch stability polling. No live work occurs on import."""
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone

from public_api_test.batch_stability import cycle_observations
from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.tests.test_freshness import stats, timestamp_statistics


def validate_symbols(symbols):
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not 1 <= len(symbols) <= 100:
        raise ValueError("multi_batch_stability requires 1..100 symbols")
    if len(set(symbols)) != len(symbols):
        raise ValueError("multi_batch_stability requires unique symbols")
    return symbols


def divide_batches(symbols, max_symbols_per_batch=20):
    """Ordered disjoint batches; 100 symbols at the default cap of 20 give 5 x 20."""
    if not isinstance(max_symbols_per_batch, int) or isinstance(max_symbols_per_batch, bool) \
            or not 1 <= max_symbols_per_batch <= 100:
        raise ValueError("max_symbols_per_batch must be an integer 1..100")
    return [symbols[i:i + max_symbols_per_batch]
            for i in range(0, len(symbols), max_symbols_per_batch)]


def monitor_multibatch(client, batches, interval, duration, emit, *, clock=time.monotonic,
                       sleep=time.sleep, wall_clock=lambda: datetime.now(timezone.utc),
                       max_age=60, tolerance=0, retain_sweeps=True, on_batch=None, should_stop=None,
                       timing_diagnostics=False):
    """Repeat full-universe sweeps at fixed intervals; one authenticated Quotes POST per batch.

    The interval applies to complete sweeps: each sweep starts at the next scheduled
    slot (aligned to the sweep cadence and the client's global rate limiter), then
    every batch runs to completion. Per-batch and sweep timing are recorded.
    With timing_diagnostics, each batch record additionally carries idle_wait_,
    postprocess_duration_ and wall_monotonic_drift_ms (idle: scheduled/rate-limit
    waits; postprocess: interval/observation processing after the operation timer
    stops, before persistence; drift: wall-clock minus monotonic span, signed).
    """
    if interval <= 0 or duration <= 0 or not math.isfinite(interval) or not math.isfinite(duration):
        raise ValueError("Interval and duration must be finite and positive")
    if not batches or any(not isinstance(b, list) or not b for b in batches):
        raise ValueError("batches must be a non-empty list of non-empty symbol lists")
    if not client.token or not client.account_id:
        raise ValueError("Initialize authentication/account once before monitoring")
    symbols = [s for batch in batches for s in batch]
    if len(set(symbols)) != len(symbols):
        raise ValueError("batches must contain unique symbols")
    start = clock()
    deadline = start + duration
    scheduled = start
    sweeps, previous = [], {}
    last_start = None
    interrupted = False
    skipped_slots = 0
    sweep_count = 0
    def wait_until(due):
        if should_stop is None:
            sleep(max(0,due-clock()))
        else:
            while clock() < due and not should_stop():
                sleep(min(0.25,due-clock()))
    try:
        while clock() < deadline and not (should_stop and should_stop()):
            due = max(scheduled, client.next_request)
            pre_wait = clock() if timing_diagnostics else None
            wait_until(min(due, deadline))
            sweep_idle = max(0.0, clock() - pre_wait) if timing_diagnostics else 0.0
            if clock() >= deadline or (should_stop and should_stop()):
                break
            sent = clock()
            sent_utc = wall_clock().astimezone(timezone.utc).isoformat()
            batch_records, observations = [], []
            for index, batch_symbols in enumerate(batches):
                idle_wait = sweep_idle
                if index > 0:
                    # The global requests-per-minute cap spans all batches and sweeps;
                    # the client's own spacing remains a second line of defense.
                    pre_wait = clock() if timing_diagnostics else None
                    wait_until(client.next_request)
                    if timing_diagnostics:
                        idle_wait = max(0.0, clock() - pre_wait)
                if should_stop and should_stop():
                    break
                batch_sent = clock()
                wall_epoch0 = wall_clock().timestamp() if timing_diagnostics else None
                batch_sent_utc = wall_clock().astimezone(timezone.utc).isoformat()
                try:
                    batch = client.get_quotes_batch(batch_symbols)  # One Quotes POST per batch.
                except Exception as error:
                    batch = dict(category="client_error", http_status=None, api_error_code=type(error).__name__,
                        quote_request_count=None, http_latency_ms=None, results=[], instrument_results=[],
                        returned_symbol_list=[], missing_symbols=list(batch_symbols))
                operation_duration_ms = (clock()-batch_sent)*1000
                postprocess0 = clock() if timing_diagnostics else None
                finished = wall_clock().astimezone(timezone.utc)
                request = next(iter(batch.get("results", [])), {})
                receive_raw = request.get("response_received_at_utc")
                received = datetime.fromisoformat(receive_raw) if receive_raw else finished
                rows = cycle_observations(batch, batch_symbols, received, previous, max_age, tolerance)
                observations.extend(rows)
                valid_count = sum(1 for item in batch.get("instrument_results", [])
                    if item.get("category") == "success")
                batch_record = dict(batch_index=index + 1,
                    symbols_requested=list(batch_symbols), batch=batch,
                    http_status=batch.get("http_status"), category=batch.get("category"),
                    http_latency_ms=batch.get("http_latency_ms"),
                    quote_request_count=batch.get("quote_request_count"),
                    api_error_code=batch.get("api_error_code"),
                    valid_price_count=valid_count,
                    completeness_ratio=valid_count / len(batch_symbols),
                    returned_symbol_list=batch.get("returned_symbol_list", []),
                    missing_symbols=batch.get("missing_symbols", []),
                    request_start_utc=batch_sent_utc,
                    response_received_at_utc=receive_raw,
                    batch_start_offset_seconds=batch_sent - sent,
                    operation_duration_monotonic_ms=operation_duration_ms)
                if timing_diagnostics:
                    batch_record.update(idle_wait_monotonic_ms=idle_wait*1000,
                        postprocess_duration_monotonic_ms=max(0.0, (clock() - postprocess0)*1000),
                        wall_monotonic_drift_ms=((wall_clock().timestamp() - wall_epoch0)
                            - (clock() - batch_sent))*1000)
                batch_records.append(batch_record)
                if on_batch is not None:
                    on_batch(client._sanitize_batch(dict(sweep=sweep_count+1,
                        expected_symbols=list(batch_symbols),observations=rows,batches=[batch_records[-1]],
                        final_batch_in_sweep=index==len(batches)-1,
                        scheduling_lateness_seconds=sent-scheduled,
                        actual_polling_interval_seconds=sent-last_start if last_start is not None else None,
                        batch_skew_seconds=batch_sent-sent)))
            if not batch_records:
                break
            sweep_count += 1
            record = dict(sweep=sweep_count,
                scheduled_offset_seconds=scheduled - start,
                actual_start_offset_seconds=sent - start,
                scheduling_lateness_seconds=sent - scheduled,
                actual_polling_interval_seconds=sent - last_start if last_start is not None else None,
                request_start_utc=sent_utc,
                request_finished_utc=wall_clock().astimezone(timezone.utc).isoformat(),
                sweep_duration_seconds=clock() - sent,
                batch_skew_seconds=batch_records[-1]["batch_start_offset_seconds"] if batch_records else 0.0,
                quote_request_count=sum(b.get("quote_request_count") or 0 for b in batch_records),
                batches=batch_records, observations=observations)
            record = client._sanitize_batch(record)
            if retain_sweeps:
                sweeps.append(record)
            emit(record)
            last_start = sent
            scheduled += interval
            if scheduled < clock():
                new_slot = start + (math.floor((clock() - start) / interval) + 1) * interval
                skipped_slots += round((new_slot - scheduled) / interval)
                scheduled = new_slot
    except KeyboardInterrupt:
        interrupted = True
    return sweeps, interrupted, clock() - start, skipped_slots


def summarize_multibatch(sweeps, symbols):
    batch_records = [b for sweep in sweeps for b in sweep["batches"]]
    observations = [o for sweep in sweeps for o in sweep["observations"]]
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
    def batch_summary(index):
        rows = [s["batches"][index] for s in sweeps if index < len(s["batches"])]
        return dict(symbols_requested=rows[0].get("symbols_requested", []) if rows else [],
            http_status_distribution=dict(Counter(str(b.get("http_status")) for b in rows)),
            http_latency_ms=latency([b["http_latency_ms"] for b in rows if b.get("http_latency_ms") is not None]),
            completeness_ratio=stats([b["completeness_ratio"] for b in rows if b.get("completeness_ratio") is not None]),
            http_429_responses=sum(b.get("http_status") == 429 for b in rows))
    return dict(total_sweeps=len(sweeps),
        successful_sweeps=sum(all(b.get("category") == "success" for b in s["batches"]) for s in sweeps),
        partial_sweeps=sum(any(b.get("category") == "success" for b in s["batches"])
            and not all(b.get("category") == "success" for b in s["batches"]) for s in sweeps),
        quotes_http_requests=sum(b.get("quote_request_count") or 0 for b in batch_records),
        http_429_responses=sum(b.get("http_status") == 429 for b in batch_records),
        network_timeouts=sum(b.get("category") == "network_error" and "Timeout" in str(b.get("api_error_code")) for b in batch_records),
        network_errors=sum(b.get("category") == "network_error" and "Timeout" not in str(b.get("api_error_code")) for b in batch_records),
        client_errors=sum(b.get("category") == "client_error" for b in batch_records),
        request_failure_batches=sum(b.get("category") not in ("success", "partial_result", "empty_or_failed_quotes")
            for b in batch_records),
        instrument_failures=sum(o["category"] not in ("success", "request_failed") for o in observations),
        missing_symbol_observations=sum(o["category"] == "missing_symbol" for o in observations),
        http_status_distribution=dict(Counter(str(b.get("http_status")) for b in batch_records)),
        http_latency_ms=latency([b["http_latency_ms"] for b in batch_records if b.get("http_latency_ms") is not None]),
        sweep_duration_seconds=stats([s["sweep_duration_seconds"] for s in sweeps]),
        batch_skew_seconds=stats([s["batch_skew_seconds"] for s in sweeps]),
        actual_polling_intervals_seconds=stats([s["actual_polling_interval_seconds"] for s in sweeps
            if s["actual_polling_interval_seconds"] is not None]),
        batches_per_sweep=[len(s["batches"]) for s in sweeps],
        timestamp_statistics=ages(observations),
        per_batch=[batch_summary(i) for i in range(max(len(s["batches"]) for s in sweeps))] if sweeps else [],
        sweep_results=[dict(sweep=s["sweep"], http_status=[b.get("http_status") for b in s["batches"]],
            category=[b.get("category") for b in s["batches"]],
            returned_symbols=[sym for b in s["batches"] for sym in b.get("returned_symbol_list", [])],
            missing_symbols=[sym for b in s["batches"] for sym in b.get("missing_symbols", [])]) for s in sweeps],
        per_symbol={symbol: dict(observations=sum(o["symbol"] == symbol for o in observations),
            timestamp_statistics=ages([o for o in observations if o["symbol"] == symbol]),
            volume_delta_statistics=stats([o["volume_delta"] for o in observations
                if o["symbol"] == symbol and o["volume_delta"] is not None]),
            volume_interval_status_counts=dict(Counter(o["volume_delta_status"] for o in observations
                if o["symbol"] == symbol)),
            volume_samples=[dict(sweep=s["sweep"], volume=o["volume"], delta=o["volume_delta"],
                status=o["volume_delta_status"])
                for s in sweeps for o in s["observations"] if o["symbol"] == symbol]) for symbol in symbols})


def run_multi_batch_stability(symbols, interval_seconds=10, duration_seconds=1800,
                              requests_per_minute=60, max_age=60, clock_skew_tolerance=0,
                              max_symbols_per_batch=20):
    """Authenticate once, then sweep the full universe as repeated batch POSTs.

    100 symbols at the default cap of 20 per batch yield five authenticated Quotes
    POST requests per sweep, repeated every interval_seconds for duration_seconds.
    """
    symbols = validate_symbols(symbols)
    if interval_seconds <= 0 or duration_seconds <= 0:
        raise ValueError("Interval and duration must be positive")
    batches = divide_batches(symbols, max_symbols_per_batch)
    client = PublicComClient(requests_per_minute=requests_per_minute)
    name = "multi_batch_stability_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = RAW_OBSERVATIONS_DIR / (name + ".jsonl")
    summary_path = SUMMARY_STATS_DIR / (name + ".json")
    try:
        setup = client._sanitize_batch(client.prepare())
        if not client.token or not client.account_id:
            summary_path.write_text(json.dumps(dict(run_status="setup_failed", setup=setup), indent=2))
            print("Multi-batch stability setup failed; no polling started. See " + str(summary_path))
            return False
        with raw_path.open("w", encoding="utf-8") as stream:
            def emit(record):
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                statuses = "/".join(str(b.get("http_status")) for b in record["batches"])
                returned = sum(len(b.get("returned_symbol_list", [])) for b in record["batches"])
                print(f"Sweep {record['sweep']}: HTTP={statuses} returned={returned}/{len(symbols)} "
                    f"requests={record['quote_request_count']} "
                    f"sweep_ms={round(record['sweep_duration_seconds'] * 1000)} "
                    f"skew_ms={round(record['batch_skew_seconds'] * 1000)}", flush=True)
            sweeps, interrupted, elapsed, skipped = monitor_multibatch(client, batches,
                interval_seconds, duration_seconds, emit, max_age=max_age, tolerance=clock_skew_tolerance)
        summary = summarize_multibatch(sweeps, symbols)
        summary.update(run_status="interrupted" if interrupted else
            "success" if sweeps and summary["successful_sweeps"] == len(sweeps) else "degraded",
            symbols=symbols, batches=[list(batch) for batch in batches],
            interval_seconds=interval_seconds, duration_seconds=duration_seconds,
            elapsed_seconds=elapsed, skipped_schedule_slots=skipped, setup=setup,
            requests_per_minute=requests_per_minute, max_age_seconds=max_age,
            clock_skew_tolerance_seconds=clock_skew_tolerance,
            max_symbols_per_batch=max_symbols_per_batch)
        summary_path.write_text(json.dumps(client._sanitize_batch(summary), indent=2), encoding="utf-8")
        print(f"Raw observations: {raw_path}\nSummary: {summary_path}")
        return summary["run_status"] == "success"
    finally:
        client.close()
