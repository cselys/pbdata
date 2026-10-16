"""Sequential, quota-paced quote cycles scheduled on a monotonic clock."""
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.sessions import annotate_quote, quote_capabilities, session_at


def stats(values):
    values = sorted(values)
    if not values:
        return None
    return dict(count=len(values), min=values[0], max=values[-1],
        mean=statistics.mean(values), median=statistics.median(values),
        p95=values[max(0, math.ceil(len(values) * .95) - 1)])


def timestamp_statistics(observations):
    eligible = [o for o in observations if o.get("category") in ("success", "missing_price")]
    result = {}
    for side in ("last", "bid", "ask"):
        ages = [o.get(f"{side}_age_seconds") for o in eligible]
        recencies = [o.get(f"{side}_data_recency_seconds") for o in eligible]
        statuses = Counter(o.get(f"{side}_timestamp_status", "missing_timestamp") for o in eligible)
        result[side] = dict(observation_count=len(eligible),
            excluded_failed_requests=len(observations)-len(eligible),
            timestamp_availability=sum(age is not None for age in ages),
            nonnegative_age_seconds=stats([age for age in ages if age is not None and age >= 0]),
            negative_age_seconds=stats([age for age in ages if age is not None and age < 0]),
            data_recency_seconds=stats([r for r in recencies if r is not None]),
            fresh_observations=sum(bool(o.get(f"{side}_freshness_verified")) for o in eligible),
            valid_timestamps=statuses["valid"], stale_observations=statuses["stale"],
            future_timestamps=statuses["future_timestamp"], missing_timestamps=statuses["missing_timestamp"],
            parse_errors=sum(bool(o.get(f"{side}_timestamp_parse_error")) for o in eligible),
            future_within_tolerance=sum(bool(o.get(f"{side}_future_within_tolerance")) for o in eligible))
    return result


def format_observation(obs):
    def age(side):
        value = obs.get(f"{side}_age_seconds")
        return "NA" if value is None else f"{value:.3f}s"
    statuses = "/".join(obs.get(f"{side}_timestamp_status", "missing_timestamp") for side in ("last", "bid", "ask"))
    return (f"[{obs['symbol']}] session={obs['market_session']} last={obs.get('price')} "
        f"last_age={age('last')} bid_age={age('bid')} ask_age={age('ask')} "
        f"http={obs.get('request_duration_ms', 0):.1f}ms status={obs.get('http_status')} "
        f"timestamps(last/bid/ask)={statuses} result={obs.get('category')}")


def summarize(observations, symbols):
    valid = [o for o in observations if o["category"] == "success"]
    failed = [o for o in observations if o["category"] != "success"]
    return dict(timestamp_statistics=timestamp_statistics(observations), total_requests=len(observations), successful_requests=len(valid),
        failed_requests=len(failed), valid_price_count=len(valid),
        http_successful_requests=sum(200 <= (o.get("http_status") or 0) < 300 for o in observations),
        http_status_distribution=dict(Counter(str(o.get("http_status")) for o in observations)),
        category_distribution=dict(Counter(o["category"] for o in observations)),
        timestamp_available_count=sum(o.get("provider_timestamp") is not None for o in valid),
        http_latency_ms_successful=stats([o["request_duration_ms"] for o in valid]),
        http_latency_ms_failed=stats([o["request_duration_ms"] for o in failed]),
        data_age_seconds=stats([o["data_age_seconds"] for o in valid if o.get("data_age_seconds") is not None]),
        per_symbol={s: dict(timestamp_statistics=timestamp_statistics([o for o in observations if o["symbol"] == s]), observations=sum(o["symbol"] == s for o in observations),
            valid_price_count=sum(o["symbol"] == s for o in valid),
            timestamp_available_count=sum(o["symbol"] == s and o.get("provider_timestamp") is not None for o in valid),
            unique_prices=len({o["price"] for o in valid if o["symbol"] == s}),
            price_changes=sum(o.get("price_changed", False) for o in valid if o["symbol"] == s),
            sampling_interval_seconds=stats([o["sampling_interval_seconds"] for o in observations
                if o["symbol"] == s and o.get("sampling_interval_seconds") is not None])) for s in symbols})


def monitor(client, symbols, interval, duration, emit, clock=time.monotonic, sleep=time.sleep,
            session="auto", max_age=60, wall_clock=lambda: datetime.now(timezone.utc), clock_skew_tolerance=0):
    start = clock()
    deadline = start + duration
    scheduled = start
    previous_sample, previous_price = {}, {}
    observations = []
    interrupted = False
    cycle = 0
    try:
        while clock() < deadline:
            sleep(max(0, min(scheduled, deadline) - clock()))
            if clock() >= deadline:
                break
            cycle += 1
            cycle_session = session_at(wall_clock())
            # Complete each started cycle; rate limits can extend its duration.
            for symbol in symbols:
                sleep(max(0, client.next_request - clock()))
                sample = clock()
                request_utc = wall_clock().isoformat()
                obs = client.get_quote(symbol)
                now = datetime.fromisoformat(obs["response_received_at_utc"]) if obs.get("response_received_at_utc") else wall_clock()
                obs.update(cycle=cycle, cycle_market_session=cycle_session, request_start_timestamp_utc=request_utc,
                    response_timestamp_utc=now.isoformat(),
                    sampling_interval_seconds=sample - previous_sample[symbol] if symbol in previous_sample else None,
                    data_age_seconds=None, price_changed=False)
                previous_sample[symbol] = sample
                if obs["category"] == "success":
                    if obs.get("provider_timestamp") is not None:
                        obs["data_age_seconds"] = now.timestamp() - obs["provider_timestamp"]
                    obs["price_changed"] = symbol in previous_price and previous_price[symbol] != obs["price"]
                    previous_price[symbol] = obs["price"]
                annotate_quote(obs, session, max_age, now, clock_skew_tolerance)
                observations.append(obs)
                emit(obs)
            scheduled += interval
            # Skip elapsed slots instead of producing catch-up bursts.
            if scheduled < clock():
                scheduled = start + (math.floor((clock() - start) / interval) + 1) * interval
    except KeyboardInterrupt:
        interrupted = True
    return observations, interrupted, clock() - start


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_freshness(symbols=None, interval_seconds=5, duration_seconds=15, requests_per_minute=60,
                   session="auto", max_age=60, clock_skew_tolerance=0):
    symbols = symbols or ["AAPL", "NVDA", "MSFT", "TSLA", "SPY"]
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    if not symbols or interval_seconds <= 0 or duration_seconds <= 0:
        raise ValueError("Symbols, interval and duration must be positive/nonempty")
    client = PublicComClient(requests_per_minute=requests_per_minute)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = "freshness_" + stamp
    try:
        setup = client.prepare()
        for record in setup:
            print(json.dumps(record))
        if not client.token or not client.account_id:
            print("FAIL: setup failed; no market-data requests sent.")
            (SUMMARY_STATS_DIR / (name + ".json")).write_text(json.dumps(
                dict(run_status="setup_failed", total_requests=0, setup=setup), indent=2))
            return False
        print(f"Interval {interval_seconds}s is between cycle starts; sequential requests capped at {requests_per_minute}/min.")
        with (RAW_OBSERVATIONS_DIR / (name + ".jsonl")).open("w") as stream:
            def emit(obs):
                line = json.dumps(obs)
                stream.write(line + "\n")
                stream.flush()
                print(format_observation(obs), flush=True)
            observations, interrupted, elapsed = monitor(client, symbols, interval_seconds, duration_seconds, emit,
                session=session, max_age=max_age, clock_skew_tolerance=clock_skew_tolerance)
        summary = summarize(observations, symbols)
        selected = [o for o in observations if o["session_filter_match"]]
        summary.update(clock_skew_tolerance_seconds=clock_skew_tolerance,
            per_session={name: summarize([o for o in observations if o["market_session"] == name], symbols)
                for name in ("premarket", "regular", "afterhours", "closed")},
            session_capabilities=quote_capabilities(observations, symbols),
            requested_session=session, selected_observations=len(selected),
            filtered_out_observations=len(observations) - len(selected),
            selected_statistics=summarize(selected, symbols),
            freshness_threshold_seconds=max_age,
            freshness_verified_observations=sum(o["freshness_verified"] for o in selected),
            session_validation_status="observed" if selected else "not_observed")
        status = "interrupted" if interrupted else ("success" if observations and summary["failed_requests"] == 0 else "failed")
        if status == "success" and not selected:
            status = "session_not_observed"
        summary.update(run_status=status, symbols=symbols, interval_seconds=interval_seconds,
            duration_seconds=duration_seconds, elapsed_seconds=elapsed, setup=setup)
        (SUMMARY_STATS_DIR / (name + ".json")).write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        return status == "success"
    finally:
        client.close()


if __name__ == "__main__":
    from public_api_test.__main__ import main
    main()
