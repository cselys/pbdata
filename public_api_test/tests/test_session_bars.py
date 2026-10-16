"""Separate historical-bars diagnostic. Bar availability never verifies live freshness."""
import json
import math
from datetime import datetime, timezone
from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.sessions import SESSIONS, session_at, timestamp, indicators

BUCKETS = {"preMarket": "premarket", "regularMarket": "regular", "afterMarket": "afterhours",
           "preMarketOvernight": "overnight", "postMarketOvernight": "overnight"}


def parse_bars(record, selected="auto", now=None):
    now = now or datetime.now(timezone.utc)
    body = record.get("raw_response", {})
    observations = []
    if record.get("category") != "success" or not isinstance(body, dict):
        return observations
    for bucket, session in BUCKETS.items():
        group = body.get(bucket)
        bars = group.get("bars", []) if isinstance(group, dict) else []
        if not isinstance(bars, list):
            continue
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            price = None
            try:
                number = float(bar.get("close"))
                if math.isfinite(number) and number > 0:
                    price = number
            except (TypeError, ValueError):
                pass
            ts = timestamp(bar.get("timestamp"))
            provider_session = session_at(datetime.fromtimestamp(ts, timezone.utc)) if ts is not None else None
            observations.append(dict(symbol=record["symbol"], data_kind="historical_bar",
                market_session=session, observed_market_session=session_at(now),
                provider_session=provider_session, api_session_indicators={"response_bucket": bucket, **indicators(bar)},
                session_indicator_matches_calendar=provider_session == session if session in SESSIONS else None,
                observed_at_utc=now.isoformat(), http_status=record["http_status"],
                price=price, price_field="close", provider_timestamp=ts,
                provider_timestamp_raw=bar.get("timestamp"), data_age_seconds=now.timestamp()-ts if ts is not None else None,
                request_duration_ms=record["request_duration_ms"], source_endpoint=record["endpoint_url"],
                request_parameters=record["request_parameters"],
                session_filter_match=selected == "auto" or selected == session,
                freshness_verified=False, freshness_reason="historical_bar_not_live_quote"))
    return observations


def bars_capabilities(records, observations, symbols):
    result = {}
    for session in SESSIONS:
        rows = [o for o in observations if o["market_session"] == session and o["session_filter_match"]]
        result[session] = dict(documented_support=True, data_kind="historical_bars",
            endpoint_accessible_with_key=any(r.get("category") == "success" for r in records),
            accessible_with_key=True if rows else None,
            session_evidence="bars_returned" if rows else "no_bars_observed_not_proof_of_unsupported_session",
            bars_returned=len(rows), price_available=any(o["price"] is not None for o in rows),
            provider_timestamp_available=any(o["provider_timestamp"] is not None for o in rows),
            freshness_verified=False, freshness_reason="historical_availability_is_not_live_quote_evidence",
            per_symbol={symbol: dict(bars_returned=sum(o["symbol"] == symbol for o in rows),
                accessible_with_key=True if any(o["symbol"] == symbol for o in rows) else None,
                price_available=any(o["symbol"] == symbol and o["price"] is not None for o in rows),
                provider_timestamp_available=any(o["symbol"] == symbol and o["provider_timestamp"] is not None for o in rows))
                for symbol in symbols})
    return result


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_session_bars(symbols, session="auto", trading_session_toggle="REGULAR_AND_EXTENDED_HOURS",
                      requests_per_minute=60):
    client = PublicComClient(requests_per_minute=requests_per_minute)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = "session_bars_" + stamp
    records, observations = [], []
    try:
        setup = client.prepare()
        for item in setup:
            print(json.dumps(item))
        if not client.token:
            print("FAIL: authentication unavailable")
            return False
        for symbol in symbols:
            record = client.get_bars_v2(symbol, trading_session_toggle)
            records.append(record)
            rows = parse_bars(record, session)
            observations.extend(rows)
            print(json.dumps({k: v for k, v in record.items() if k != "raw_response"}))
            print(json.dumps(dict(symbol=symbol, parsed_bars=len(rows))))
        (RAW_OBSERVATIONS_DIR / (name + ".json")).write_text(json.dumps(
            dict(requests=records, observations=observations), indent=2))
        summary = dict(data_kind="historical_bars", total_requests=len(records),
            successful_requests=sum(r["category"] == "success" for r in records),
            requested_session=session, trading_session_toggle=trading_session_toggle,
            session_capabilities=bars_capabilities(records, observations, symbols))
        summary["failed_requests"] = len(records) - summary["successful_requests"]
        summary["selected_bars"] = sum(o["session_filter_match"] for o in observations)
        summary["run_status"] = "success" if records and not summary["failed_requests"] and summary["selected_bars"] else "unverified"
        (SUMMARY_STATS_DIR / (name + ".json")).write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        return summary["run_status"] == "success"
    finally:
        client.close()
