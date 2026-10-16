"""RTH (Regular Trading Hours) data-quality validation test for Public.com API.

Validates market-data freshness, timestamp advancement, bid/ask consistency,
volume tracking, and polling cadence during Regular Trading Hours (9:30-16:00 ET).

References:
- Finnhub observations: Mean data age ~19.12s (AAPL), ~15.06s (NVDA) at 5s polling for 300s.
- This project uses consistent definitions of data age and HTTP latency.
"""

import sys
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.sessions import session_at, annotate_quote, boundaries
from public_api_test.tests.test_freshness import monitor, summarize, stats

ET = ZoneInfo("America/New_York")


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def repeat_counts(rows, session=None):
    """Count adjacent successful samples within one symbol/session/local date.

    Failures and session boundaries break comparisons. The legacy full-response
    metric compares price, bid, ask and sizes (not transport metadata).
    """
    frozen = unchanged = 0
    fields = ("price", "bid", "ask", "bid_size", "ask_size")
    for prev, cur in zip(rows, rows[1:]):
        if any(o.get("category") != "success" for o in (prev, cur)):
            continue
        if prev.get("symbol") != cur.get("symbol") or prev.get("market_session") != cur.get("market_session"):
            continue
        if session is not None and cur.get("market_session") != session:
            continue
        dates = []
        for row in (prev, cur):
            raw = row.get("observed_at_utc")
            dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET).date() if raw else None)
        if dates[0] != dates[1]:
            continue
        if all(o.get("price") is not None and o.get("provider_timestamp") is not None for o in (prev, cur)):
            frozen += prev["price"] == cur["price"] and prev["provider_timestamp"] == cur["provider_timestamp"]
        if all(o.get("price") is not None for o in (prev, cur)):
            unchanged += all(prev.get(k) == cur.get(k) for k in fields)
    return frozen, unchanged


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_rth_quality(
    symbols=None,
    interval_seconds=5,
    duration_seconds=300,
    requests_per_minute=60,
    session="auto",
    max_age=60,
    clock_skew_tolerance=0,
    bars_trading_session_toggle="REGULAR_AND_EXTENDED_HOURS",
):
    """Run RTH data-quality validation monitoring.

    Parameters
    ----------
    symbols : list[str]
        Symbols to monitor.
    interval_seconds : int
        Polling interval in seconds (default 5).
    duration_seconds : int
        Test duration in seconds (default 300 = 5 min baseline; 1800 = 30 min stability).
    requests_per_minute : int
        Local request rate cap.
    session : str
        Session filter: "auto", "premarket", "regular", "afterhours".
    max_age : float
        Data-age threshold in seconds for freshness validation.
    clock_skew_tolerance : float
        Tolerance for small future timestamps; never counts them as proven fresh.
    bars_trading_session_toggle : str
        Session toggle for bar requests (not used in live quote monitoring).

    Returns
    -------
    bool
        True if the test completed without critical errors.
    """
    if symbols is None:
        symbols = ["AAPL", "NVDA", "MSFT", "TSLA", "SPY"]
    symbols = [s.upper() for s in symbols]

    print(f"RTH Quality Test: symbols={','.join(symbols)}, interval={interval_seconds}s, duration={duration_seconds}s")
    print(f"RTH window: 9:30-16:00 ET, requests/min={requests_per_minute}, max_age={max_age}s")

    client = PublicComClient(requests_per_minute=requests_per_minute)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"rth_quality_{'_'.join(symbols)}_{stamp}"

    records, observations = [], []
    try:
        setup = client.prepare()
        for item in setup:
            records.append(item)
            print(json.dumps(item))

        if not client.token:
            print("FAIL: authentication unavailable")
            return False

        # Run monitoring loop
        observations, interrupted, elapsed = monitor(
            client,
            symbols,
            interval_seconds,
            duration_seconds,
            emit=lambda obs: observations.append(obs),
            session=session,
            max_age=max_age,
            clock_skew_tolerance=clock_skew_tolerance,
        )

        # ------------------------------------------------------------------
        # Post-processing: per-symbol and overall quality reports
        # ------------------------------------------------------------------
        # Build per-symbol summaries
        per_symbol = {}
        for sym in symbols:
            sym_obs = [o for o in observations if o["symbol"] == sym]
            if not sym_obs:
                per_symbol[sym] = {
                    "observations": 0,
                    "successful_requests": 0,
                    "http_success_rate": None,
                    "data_age_stats": None,
                    "timestamp_status_counts": {},
                    "price_change_count": 0,
                    "frozen_timestamp_count": 0,
                    "unchanged_full_response_count": 0,
                    "missed_cycle_count": 0,
                    "volume_delta_stats": None,
                    "rate_limit_429_count": 0,
                }
                continue

            success_obs = [o for o in sym_obs if o.get("category") == "success"]
            http_success = sum(200 <= (o.get("http_status") or 0) < 300 for o in sym_obs)
            age_values = [o["data_age_seconds"] for o in success_obs if o.get("data_age_seconds") is not None and math.isfinite(o["data_age_seconds"])]
            timestamp_status_counts = Counter(o.get("last_timestamp_status", "unknown") for o in sym_obs)
            price_changes = sum(o.get("price_changed", False) for o in success_obs)

            frozen_count, unchanged_full = repeat_counts(sym_obs)

            # Volume delta analysis
            volume_deltas = []
            for i in range(1, len(sym_obs)):
                prev_vol = sym_obs[i - 1].get("volume")
                cur_vol = sym_obs[i].get("volume")
                if finite(prev_vol) and finite(cur_vol) and all(o.get("category") == "success" for o in sym_obs[i-1:i+1]):
                    volume_deltas.append(cur_vol - prev_vol)
            
            # Session-separated statistics
            regular_obs = [o for o in success_obs if o.get("market_session") == "regular"]
            afterhours_obs = [o for o in success_obs if o.get("market_session") == "afterhours"]
            
            regular_age_values = [o["data_age_seconds"] for o in regular_obs if o.get("data_age_seconds") is not None and math.isfinite(o["data_age_seconds"])]
            afterhours_age_values = [o["data_age_seconds"] for o in afterhours_obs if o.get("data_age_seconds") is not None and math.isfinite(o["data_age_seconds"])]
            
            regular_frozen, regular_unchanged = repeat_counts(sym_obs, "regular")
            afterhours_frozen, afterhours_unchanged = repeat_counts(sym_obs, "afterhours")

            # Volume deltas by session
            regular_volume_deltas = []
            for i in range(1, len(regular_obs)):
                prev_vol = regular_obs[i - 1].get("volume")
                cur_vol = regular_obs[i].get("volume")
                if finite(prev_vol) and finite(cur_vol):
                    regular_volume_deltas.append(cur_vol - prev_vol)
                    
            afterhours_volume_deltas = []
            for i in range(1, len(afterhours_obs)):
                prev_vol = afterhours_obs[i - 1].get("volume")
                cur_vol = afterhours_obs[i].get("volume")
                if finite(prev_vol) and finite(cur_vol):
                    afterhours_volume_deltas.append(cur_vol - prev_vol)
            session_boundary_volume_deltas = []
            for i in range(1, len(sym_obs)):
                prev_session = sym_obs[i - 1].get("market_session")
                cur_session = sym_obs[i].get("market_session")
                prev_vol = sym_obs[i - 1].get("volume")
                cur_vol = sym_obs[i].get("volume")
                if prev_session != cur_session and finite(prev_vol) and finite(cur_vol) and all(o.get("category") == "success" for o in sym_obs[i-1:i+1]):
                    # Volume delta crossing session boundary
                    session_boundary_volume_deltas.append({
                        "from_session": prev_session,
                        "to_session": cur_session,
                        "volume_delta": cur_vol - prev_vol,
                        "timestamp": sym_obs[i].get("observed_at_utc"),
                        "symbol": sym
                    })
            # Flag exceptionally large volume changes around 16:00 close
            large_volume_changes_near_close = []
            for i in range(1, len(sym_obs)):
                obs_time_str = sym_obs[i].get("observed_at_utc")
                if obs_time_str:
                    try:
                        obs_time = datetime.fromisoformat(obs_time_str.replace('Z', '+00:00')).astimezone(ET)
                        limits = boundaries(obs_time.date())
                        if limits is None:
                            continue
                        close_time = limits[2]
                        close_window_start = close_time - timedelta(minutes=5)
                        close_window_end = close_time + timedelta(minutes=5)
                        prev_vol = sym_obs[i - 1].get("volume")
                        cur_vol = sym_obs[i].get("volume")
                        if (close_window_start <= obs_time <= close_window_end and 
                            prev_vol is not None and cur_vol is not None and
                            math.isfinite(prev_vol) and math.isfinite(cur_vol)):
                            volume_delta = abs(cur_vol - prev_vol)
                            # Flag as exceptionally large if > 1M shares
                            if volume_delta > 1_000_000:
                                large_volume_changes_near_close.append({
                                    "timestamp": obs_time_str,
                                    "volume_delta": cur_vol - prev_vol,
                                    "previous_volume": prev_vol,
                                    "current_volume": cur_vol,
                                    "symbol": sym
                                })
                    except (ValueError, AttributeError):
                        pass  # Skip if timestamp parsing fails

            # Rate-limit 429 count
            rl_429 = sum(1 for o in sym_obs if o.get("http_status") == 429)

            per_symbol[sym] = dict(
                regular_session=dict(observations=len(regular_obs), successful_requests=len(regular_obs),
                    data_age_stats=stats(regular_age_values), frozen_timestamp_count=regular_frozen,
                    unchanged_full_response_count=regular_unchanged, volume_delta_stats=stats(regular_volume_deltas)),
                after_hours=dict(observations=len(afterhours_obs), successful_requests=len(afterhours_obs),
                    data_age_stats=stats(afterhours_age_values), frozen_timestamp_count=afterhours_frozen,
                    unchanged_full_response_count=afterhours_unchanged, volume_delta_stats=stats(afterhours_volume_deltas)),
                session_boundary_volume_deltas=session_boundary_volume_deltas,
                large_volume_changes_near_close=large_volume_changes_near_close,
                observations=len(sym_obs),
                successful_requests=len(success_obs),
                http_success_rate=http_success / len(sym_obs) if sym_obs else None,
                data_age_stats=stats(age_values) if age_values else None,
                timestamp_status_counts=dict(timestamp_status_counts),
                price_change_count=price_changes,
                frozen_timestamp_count=frozen_count,
                unchanged_full_response_count=unchanged_full,
                missed_cycle_count=sum(1 for o in sym_obs if o.get("missed_cycle", False)),
                volume_delta_stats=stats(volume_deltas) if volume_deltas else None,
                rate_limit_429_count=rl_429,
            )

        all_success = [o for o in observations if o.get("category") == "success"]
        all_age_values = [o["data_age_seconds"] for o in all_success if finite(o.get("data_age_seconds"))]
        all_ts_status = Counter(o.get("last_timestamp_status", "unknown") for o in observations)
        all_price_changes = sum(v["price_change_count"] for v in per_symbol.values())
        # Never compare interleaved symbols with one another.
        all_frozen = sum(v["frozen_timestamp_count"] for v in per_symbol.values())
        all_unchanged_full = sum(v["unchanged_full_response_count"] for v in per_symbol.values())
        all_vol_deltas = []
        for sym in symbols:
            rows = [o for o in observations if o["symbol"] == sym]
            for prev, cur in zip(rows, rows[1:]):
                if all(o.get("category") == "success" and finite(o.get("volume")) for o in (prev, cur)):
                    all_vol_deltas.append(cur["volume"] - prev["volume"])

        # Format overall stats safely
        if all_age_values:
            ad_stats = stats(all_age_values)
            ad_mean = ad_stats["mean"]
            ad_p95 = ad_stats["p95"]
        else:
            ad_mean = ad_p95 = "unverified"

        vol_delta_stats_val = stats(all_vol_deltas) if all_vol_deltas else None
        vol_p95 = f"{vol_delta_stats_val['p95']:.1f}" if vol_delta_stats_val else "unverified"

        overall = dict(
            duration_seconds=elapsed,
            total_observations=len(observations),
            total_successful=len(all_success),
            total_requests=len(observations),
            http_success_rate=sum(200 <= (o.get("http_status") or 0) < 300 for o in observations) / len(observations) if observations else None,
            data_age_stats=stats(all_age_values) if all_age_values else None,
            timestamp_status_counts=dict(all_ts_status),
            price_change_count=all_price_changes,
            frozen_timestamp_count=all_frozen,
            unchanged_full_response_count=all_unchanged_full,
            rate_limit_429_count=sum(1 for o in observations if o.get("http_status") == 429),
            per_symbol=per_symbol,
            volume_delta_stats=vol_delta_stats_val,
            per_session={name: summarize([o for o in observations if o.get("market_session") == name], symbols)
                for name in sorted({o.get("market_session", "unknown") for o in observations})},
            session_boundary_volume_deltas=[v for p in per_symbol.values() for v in p.get("session_boundary_volume_deltas", [])],
            large_volume_changes_near_close=[v for p in per_symbol.values() for v in p.get("large_volume_changes_near_close", [])],
            interrupted=interrupted,
            clock_skew_tolerance_seconds=clock_skew_tolerance,
            max_age_seconds=max_age,
            session_filter=session,
        )

        for session_name, session_report in overall["per_session"].items():
            counts = [repeat_counts([o for o in observations if o["symbol"] == sym], session_name)
                      for sym in symbols]
            session_report["frozen_timestamp_count"] = sum(c[0] for c in counts)
            session_report["unchanged_full_response_count"] = sum(c[1] for c in counts)

        # Save raw observations JSONL
        raw_path = RAW_OBSERVATIONS_DIR / f"{name}.jsonl"
        with open(raw_path, "w") as f:
            for o in observations:
                f.write(json.dumps(o) + "\n")
        print(f"Raw observations saved to: {raw_path}")

        # Save summary JSON
        summary_path = SUMMARY_STATS_DIR / f"{name}.json"
        with open(summary_path, "w") as f:
            json.dump(overall, f, indent=2, default=str)
        print(f"Summary statistics saved to: {summary_path}")

        # Print human-readable summary
        print("\n" + "=" * 60)
        print("RTH Quality Summary")
        print("=" * 60)
        print(f"Duration: {elapsed:.1f}s ({duration_seconds}s requested)")
        print(f"Total observations: {len(observations)}")
        print(f"Successful requests: {len(all_success)}")
        if all_age_values:
            print(f"Data age - mean: {ad_mean:.1f}s, p95: {ad_p95:.1f}s")
        print(f"Timestamp status distribution: {dict(all_ts_status)}")
        print(f"Price changes observed: {all_price_changes}")
        print(f"Frozen timestamps (unchanged price+TS): {all_frozen}")
        print(f"Unchanged full responses: {all_unchanged_full}")
        print(f"Rate-limit 429 responses: {sum(1 for o in observations if o.get('http_status') == 429)}")
        print("\nPer-symbol details:")
        for sym, pd in per_symbol.items():
            dvs = ""
            if pd["volume_delta_stats"]:
                dvs = f", vol_p95_delta={pd['volume_delta_stats']['p95']:.1f}"
            age_str = f"{pd['data_age_stats']['mean']:.1f}" if pd['data_age_stats'] else 'N/A'
            print(f"  {sym}: obs={pd['observations']}, success={pd['successful_requests']}, "
                  f"age_mean={age_str}, 429={pd['rate_limit_429_count']}{dvs}")

        # Suitability classification
        print("\nSuitability Classification:")
        print(f"  - Data age mean={ad_mean}s, p95={ad_p95}s")
        print(f"  - Frozen timestamps: {all_frozen} ({100*all_frozen/len(all_success) if all_success else 0:.1f}%)")
        print(f"  - Unchanged full responses: {all_unchanged_full} ({100*all_unchanged_full/len(all_success) if all_success else 0:.1f}%)")

        # Critical issues that question suitability
        critical_issues = []
        if all_age_values and stats(all_age_values).get("mean", 0) > max_age * 2:
            critical_issues.append(f"mean data age ({stats(all_age_values)['mean']:.1f}s) exceeds 2x threshold ({max_age}s)")
        if all_ts_status.get("future_timestamp", 0) > 0:
            critical_issues.append(f"future timestamps detected ({all_ts_status['future_timestamp']})")
        if all_frozen > len(all_success) * 0.3:
            critical_issues.append(f"high frozen-timestamp rate ({all_frozen}/{len(all_success)})")
        if critical_issues:
            print("  Critical issues:")
            for issue in critical_issues:
                print(f"    - {issue}")
        else:
            print("  - No critical issues detected")

        success = bool(all_success) and not interrupted
    except KeyboardInterrupt:
        print("\nRTH quality test interrupted by user")
        success = False
    finally:
        client.close()

    return success


def main_cli():
    """Standalone CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="Public.com RTH Data-Quality Validation")
    parser.add_argument("--symbols", default="AAPL,NVDA,MSFT,TSLA,SPY")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--duration", type=int, default=300, help="300=baseline, 1800=stability")
    parser.add_argument("--requests-per-minute", type=int, default=60)
    parser.add_argument("--session", choices=["auto", "premarket", "regular", "afterhours"], default="auto")
    parser.add_argument("--max-age", type=float, default=60)
    parser.add_argument("--clock-skew-tolerance", type=float, default=0)
    parser.add_argument("--bars-trading-session-toggle", choices=["REGULAR_HOURS", "REGULAR_AND_EXTENDED_HOURS", "ALL_SESSIONS"], default="REGULAR_AND_EXTENDED_HOURS")
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    success = test_rth_quality(
        symbols=symbols,
        interval_seconds=args.interval,
        duration_seconds=args.duration,
        requests_per_minute=args.requests_per_minute,
        session=args.session,
        max_age=args.max_age,
        clock_skew_tolerance=args.clock_skew_tolerance,
        bars_trading_session_toggle=args.bars_trading_session_toggle,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main_cli()