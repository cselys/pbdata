"""Quote diagnostics test — focused single-symbol-per-sweep diagnostics for quote timestamp analysis.

This module implements a diagnostic test for Public.com quote timestamps. It is
intentionally narrow: it records each field independently and produces
observations that let us distinguish between explanations we can test
empirically (unchanged timestamps, price/timestamp/volume change patterns,
session effects, network error correlation) from explanations that require
provider-side clarification (what lastTimestamp semantically represents,
session-specific quote behavior, freshness guarantees).

No live API calls occur on import. No production code is modified.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from public_api_test.client import PublicComClient
from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.sessions import session_at, timestamp as _ts_timestamp


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "FDX", "BLK", "CDNS", "PNC", "HON", "DE"]
DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_DURATION_SECONDS = 1800
DEFAULT_REQUESTS_PER_MINUTE = 40
DEFAULT_MAX_AGE_SECONDS = 60
DEFAULT_CLOCK_SKEW_TOLERANCE = 0.0
DEFAULT_MAX_SYMBOLS_PER_BATCH = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_symbols(symbols: List[str]) -> List[str]:
    """Validate and normalize symbols (unique, non-empty, uppercase)."""
    syms = [s.strip().upper() for s in symbols if s.strip()]
    if not 1 <= len(syms) <= 100:
        raise ValueError("quote_diagnostics requires 1..100 symbols")
    if len(set(syms)) != len(syms):
        raise ValueError("quote_diagnostics requires unique symbols")
    return syms


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[float]:
    """Parse an ISO 8601 timestamp string into a Unix epoch float (seconds).

    Returns None for None/empty/invalid/unparseable values. Never guesses
    units or timezone for missing values.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Observation model
# ---------------------------------------------------------------------------


class Observation:
    """A single symbol observation from one sweep."""

    def __init__(self, symbol: str, sweep: int, scheduled_offset: float,
                 actual_start_offset: float, request_start_utc: str,
                 request_finished_utc: str, http_status: Optional[int],
                 http_latency_ms: Optional[float], batch_index: int,
                 symbols_in_batch: int, category: str,
                 last_price: Optional[float], last_timestamp_raw: Optional[str],
                 last_age_seconds: Optional[float], last_timestamp_status: str,
                 bid_price: Optional[float], bid_timestamp_raw: Optional[str],
                 bid_age_seconds: Optional[float], bid_timestamp_status: str,
                 ask_price: Optional[float], ask_timestamp_raw: Optional[str],
                 ask_age_seconds: Optional[float], ask_timestamp_status: str,
                 volume: Optional[float], volume_changed: Optional[bool],
                 price_changed: bool, last_timestamp_changed: bool,
                 batch_timestamp_advanced: bool, market_session: str,
                 provider_session: Optional[str],
                 request_category: str, api_error_code: Optional[str],
                 missing_symbols: List[str]):
        self.symbol = symbol
        self.sweep = sweep
        self.scheduled_offset = scheduled_offset
        self.actual_start_offset = actual_start_offset
        self.request_start_utc = request_start_utc
        self.request_finished_utc = request_finished_utc
        self.http_status = http_status
        self.http_latency_ms = http_latency_ms
        self.batch_index = batch_index
        self.symbols_in_batch = symbols_in_batch
        self.category = category
        self.last_price = last_price
        self.last_timestamp_raw = last_timestamp_raw
        self.last_age_seconds = last_age_seconds
        self.last_timestamp_status = last_timestamp_status
        self.bid_price = bid_price
        self.bid_timestamp_raw = bid_timestamp_raw
        self.bid_age_seconds = bid_age_seconds
        self.bid_timestamp_status = bid_timestamp_status
        self.ask_price = ask_price
        self.ask_timestamp_raw = ask_timestamp_raw
        self.ask_age_seconds = ask_age_seconds
        self.ask_timestamp_status = ask_timestamp_status
        self.volume = volume
        self.volume_changed = volume_changed
        self.price_changed = price_changed
        self.last_timestamp_changed = last_timestamp_changed
        self.batch_timestamp_advanced = batch_timestamp_advanced
        self.market_session = market_session
        self.provider_session = provider_session
        self.request_category = request_category
        self.api_error_code = api_error_code
        self.missing_symbols = missing_symbols

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "sweep": self.sweep,
            "scheduled_offset_seconds": self.scheduled_offset,
            "actual_start_offset_seconds": self.actual_start_offset,
            "request_start_utc": self.request_start_utc,
            "request_finished_utc": self.request_finished_utc,
            "http_status": self.http_status,
            "http_latency_ms": self.http_latency_ms,
            "batch_index": self.batch_index,
            "symbols_in_batch": self.symbols_in_batch,
            "category": self.category,
            "last_price": self.last_price,
            "last_timestamp_raw": self.last_timestamp_raw,
            "last_age_seconds": self.last_age_seconds,
            "last_timestamp_status": self.last_timestamp_status,
            "bid_price": self.bid_price,
            "bid_timestamp_raw": self.bid_timestamp_raw,
            "bid_age_seconds": self.bid_age_seconds,
            "bid_timestamp_status": self.bid_timestamp_status,
            "ask_price": self.ask_price,
            "ask_timestamp_raw": self.ask_timestamp_raw,
            "ask_age_seconds": self.ask_age_seconds,
            "ask_timestamp_status": self.ask_timestamp_status,
            "volume": self.volume,
            "volume_changed": self.volume_changed,
            "price_changed": self.price_changed,
            "last_timestamp_changed": self.last_timestamp_changed,
            "batch_timestamp_advanced": self.batch_timestamp_advanced,
            "market_session": self.market_session,
            "provider_session": self.provider_session,
            "request_category": self.request_category,
            "api_error_code": self.api_error_code,
            "missing_symbols": self.missing_symbols,
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def _stats(values: List[float]) -> Optional[Dict[str, Any]]:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    sorted_v = sorted(clean)
    n = len(sorted_v)
    pct = lambda q: sorted_v[int(q * (n - 1))]
    return {
        "count": n,
        "min": min(sorted_v),
        "max": max(sorted_v),
        "mean": sum(sorted_v) / n,
        "median": sorted_v[n // 2],
        "p95": pct(0.95),
    }


def _percent(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator * 100.0


# ---------------------------------------------------------------------------
# Core test logic
# ---------------------------------------------------------------------------


def run_quote_diagnostics(
    symbols: List[str],
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    max_age: float = DEFAULT_MAX_AGE_SECONDS,
    clock_skew_tolerance: float = DEFAULT_CLOCK_SKEW_TOLERANCE,
    max_symbols_per_batch: int = DEFAULT_MAX_SYMBOLS_PER_BATCH,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    client: Optional[PublicComClient] = None,
) -> bool:
    """Run the quote diagnostics test.

    Returns True if the test completed without failing (non-fatal errors are
    recorded but do not raise), False otherwise.

    An optional pre-configured client can be injected for testing.
    """
    symbols = validate_symbols(symbols)
    if interval_seconds <= 0 or duration_seconds <= 0:
        raise ValueError("Interval and duration must be positive")

    if client is not None:
        c = client
    else:
        c = PublicComClient(requests_per_minute=requests_per_minute)
    client = c
    name = "quote_diagnostics_" + wall_clock().strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = RAW_OBSERVATIONS_DIR / (name + ".jsonl")
    summary_path = SUMMARY_STATS_DIR / (name + ".json")

    setup_failed = False
    setup_records: List[Dict[str, Any]] = []
    try:
        setup = client._sanitize_batch(client.prepare())
        if not client.token or not client.account_id:
            setup_failed = True
            summary_path.write_text(
                json.dumps(dict(run_status="setup_failed", setup=setup), indent=2),
                encoding="utf-8",
            )
            print("Quote diagnostics setup failed; no polling started.")
            return False
    except Exception as exc:
        setup_failed = True
        setup_records.append({"error_type": type(exc).__name__})

    observations: List[Observation] = []
    previous: Dict[str, Dict[str, Any]] = {}

    start = clock()
    deadline = start + duration_seconds
    scheduled = start
    sweep_count = 0
    interrupted = False

    try:
        while clock() < deadline:
            due = max(scheduled, client.next_request)
            sleep(max(0, min(due, deadline) - clock()))
            if clock() >= deadline:
                break
            sent = clock()
            request_start_utc = wall_clock().isoformat()
            sweep_count += 1

            # Split symbols into batches
            chunks = [
                symbols[i:i + max_symbols_per_batch]
                for i in range(0, len(symbols), max_symbols_per_batch)
            ]

            sweep_observations: List[Observation] = []
            request_finished_utc = ""
            batch_start_offset_seconds = 0.0

            for batch_index, batch_symbols in enumerate(chunks):
                try:
                    batch = client.get_quotes_batch(batch_symbols)
                except Exception as exc:
                    batch = dict(
                        category="client_error",
                        http_status=None,
                        api_error_code=type(exc).__name__,
                        quote_request_count=None,
                        http_latency_ms=None,
                        results=[],
                        instrument_results=[],
                        returned_symbol_list=[],
                        missing_symbols=list(batch_symbols),
                    )

                if request_finished_utc == "":
                    # Capture the first batch's request finished time as sweep time
                    pass

                batch_http_status = batch.get("http_status")
                batch_category = batch.get("category")
                batch_latency = batch.get("http_latency_ms")
                request_finished_utc = wall_clock().isoformat()
                batch_start_offset_seconds = request_finished_utc

                instrument_results = batch.get("instrument_results", [])
                instrument_map = {r.get("symbol"): r for r in instrument_results if isinstance(r, dict)}
                returned_symbols = set(batch.get("returned_symbol_list", []))
                missing_symbols = list(batch.get("missing_symbols", []))

                # Build per-symbol observations from the batch
                for symbol in batch_symbols:
                    instr = instrument_map.get(symbol, {})
                    raw_response = instr.get("raw_response", {})

                    last_price = _safe_float(instr.get("price"))
                    bid_price = _safe_float(raw_response.get("bid"))
                    ask_price = _safe_float(raw_response.get("ask"))

                    last_ts_raw = raw_response.get("lastTimestamp") or raw_response.get("last_timestamp")
                    bid_ts_raw = raw_response.get("bidTimestamp")
                    ask_ts_raw = raw_response.get("askTimestamp")

                    last_age = _parse_ts(last_ts_raw) if last_ts_raw else None
                    bid_age = _parse_ts(bid_ts_raw) if bid_ts_raw else None
                    ask_age = _parse_ts(ask_ts_raw) if ask_ts_raw else None

                    received = request_finished_utc
                    received_dt = datetime.fromisoformat(received.replace("Z", "+00:00")) if "Z" in received else datetime.fromisoformat(received)
                    if received_dt.tzinfo is None:
                        received_dt = received_dt.replace(tzinfo=timezone.utc)

                    last_age_sec = received_dt.timestamp() - last_age if last_age is not None else None
                    bid_age_sec = received_dt.timestamp() - bid_age if bid_age is not None else None
                    ask_age_sec = received_dt.timestamp() - ask_age if ask_age is not None else None

                    # Status classification
                    def _status(age):
                        if age is None:
                            return "missing_timestamp"
                        if age < 0:
                            return "future_timestamp"
                        if age > max_age:
                            return "stale"
                        return "valid"

                    last_ts_status = _status(last_age_sec)
                    bid_ts_status = _status(bid_age_sec)
                    ask_ts_status = _status(ask_age_sec)

                    # Volume and price change detection
                    prev = previous.get(symbol, {})
                    prev_price = prev.get("last_price")
                    prev_volume = prev.get("volume")
                    prev_last_ts = prev.get("last_timestamp_raw")
                    prev_bid_ts = prev.get("bid_timestamp_raw")
                    prev_ask_ts = prev.get("ask_timestamp_raw")
                    prev_last_price = prev.get("last_price")
                    prev_last_age_sec = prev.get("last_age_seconds")
                    prev_bid_age_sec = prev.get("bid_age_seconds")
                    prev_ask_age_sec = prev.get("ask_age_seconds")

                    price_changed = (
                        prev_last_price is not None and last_price is not None
                        and prev_last_price != last_price
                    )
                    volume_changed = (
                        prev_volume is not None and _safe_float(raw_response.get("volume")) is not None
                        and prev_volume != _safe_float(raw_response.get("volume"))
                    )
                    last_timestamp_changed = (
                        prev_last_ts is not None and last_ts_raw is not None
                        and prev_last_ts != last_ts_raw
                    )
                    batch_timestamp_advanced = (
                        (prev_bid_age_sec is not None and bid_age_sec is not None
                         and prev_bid_age_sec != bid_age_sec)
                        or (prev_ask_age_sec is not None and ask_age_sec is not None
                            and prev_ask_age_sec != ask_age_sec)
                    )

                    http_status = batch_http_status
                    http_latency = batch_latency
                    category = batch_category

                    market_session = session_at(received_dt) if received_dt.tzinfo else "auto"
                    provider_session = session_at(datetime.fromtimestamp(last_age, timezone.utc)) if last_age else None

                    obs = Observation(
                        symbol=symbol,
                        sweep=sweep_count,
                        scheduled_offset=scheduled - start,
                        actual_start_offset=sent - start,
                        request_start_utc=request_start_utc,
                        request_finished_utc=request_finished_utc,
                        http_status=http_status,
                        http_latency_ms=http_latency,
                        batch_index=batch_index,
                        symbols_in_batch=len(batch_symbols),
                        category=category,
                        last_price=last_price,
                        last_timestamp_raw=last_ts_raw,
                        last_age_seconds=last_age_sec,
                        last_timestamp_status=last_ts_status,
                        bid_price=bid_price,
                        bid_timestamp_raw=bid_ts_raw,
                        bid_age_seconds=bid_age_sec,
                        bid_timestamp_status=bid_ts_status,
                        ask_price=ask_price,
                        ask_timestamp_raw=ask_ts_raw,
                        ask_age_seconds=ask_age_sec,
                        ask_timestamp_status=ask_ts_status,
                        volume=_safe_float(raw_response.get("volume")),
                        volume_changed=volume_changed,
                        price_changed=price_changed,
                        last_timestamp_changed=last_timestamp_changed,
                        batch_timestamp_advanced=batch_timestamp_advanced,
                        market_session=market_session,
                        provider_session=provider_session,
                        request_category=category,
                        api_error_code=batch.get("api_error_code"),
                        missing_symbols=missing_symbols,
                    )
                    sweep_observations.append(obs)
                    previous[symbol] = {
                        "last_price": last_price,
                        "volume": _safe_float(raw_response.get("volume")),
                        "last_timestamp_raw": last_ts_raw,
                        "bid_timestamp_raw": bid_ts_raw,
                        "ask_timestamp_raw": ask_ts_raw,
                        "last_age_seconds": last_age_sec,
                        "bid_age_seconds": bid_age_sec,
                        "ask_age_seconds": ask_age_sec,
                    }

                observations.extend(sweep_observations)

                # Record completion for this batch in the emit callback below

            scheduled += interval_seconds

        # Build summary
        summary = _build_summary(
            observations, symbols, sweep_count, interrupted, clock() - start,
            interval_seconds, duration_seconds, requests_per_minute,
            max_age, clock_skew_tolerance, max_symbols_per_batch,
        )
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Raw observations: {raw_path}\nSummary: {summary_path}")
        return summary.get("run_status") == "success"

    except KeyboardInterrupt:
        interrupted = True
        # Write partial summary
        summary = _build_summary(
            observations, symbols, sweep_count, True, clock() - start,
            interval_seconds, duration_seconds, requests_per_minute,
            max_age, clock_skew_tolerance, max_symbols_per_batch,
        )
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Interrupted. Partial summary: {summary_path}")
        return False
    finally:
        client.close()


def _build_summary(
    observations: List[Observation],
    symbols: List[str],
    sweep_count: int,
    interrupted: bool,
    elapsed: float,
    interval_seconds: int,
    duration_seconds: int,
    requests_per_minute: int,
    max_age: float,
    clock_skew_tolerance: float,
    max_symbols_per_batch: int,
) -> Dict[str, Any]:
    """Build the summary statistics from observations."""
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        obs_list = [o for o in observations if o.symbol == symbol]
        last_ages = [o.last_age_seconds for o in obs_list if o.last_age_seconds is not None]
        bid_ages = [o.bid_age_seconds for o in obs_list if o.bid_age_seconds is not None]
        ask_ages = [o.ask_age_seconds for o in obs_list if o.ask_age_seconds is not None]

        stale_last = sum(1 for o in obs_list if o.last_timestamp_status == "stale")
        future_last = sum(1 for o in obs_list if o.last_timestamp_status == "future_timestamp")
        missing_last = sum(1 for o in obs_list if o.last_timestamp_status == "missing_timestamp")
        valid_last = sum(1 for o in obs_list if o.last_timestamp_status == "valid")

        stale_bid = sum(1 for o in obs_list if o.bid_timestamp_status == "stale")
        stale_ask = sum(1 for o in obs_list if o.ask_timestamp_status == "stale")

        last_ts_advanced = sum(1 for o in obs_list if o.last_timestamp_changed)
        last_price_changed = sum(1 for o in obs_list if o.price_changed)
        volume_changed = sum(1 for o in obs_list if o.volume_changed is True)

        # Consecutive stale sequences
        max_consec_stale = 0
        current_streak = 0
        for o in obs_list:
            if o.last_timestamp_status == "stale":
                current_streak += 1
                max_consec_stale = max(max_consec_stale, current_streak)
            else:
                current_streak = 0

        # Bid/Ask fresh when Last stale
        bid_ask_fresh_when_last_stale = sum(
            1 for o in obs_list
            if o.last_timestamp_status == "stale"
            and o.bid_timestamp_status == "valid"
            and o.ask_timestamp_status == "valid"
        )

        price_changed_when_last_ts_not = sum(
            1 for o in obs_list
            if not o.last_timestamp_changed and o.price_changed
        )

        per_symbol[symbol] = {
            "observations": len(obs_list),
            "stale_last": stale_last,
            "future_last": future_last,
            "missing_last": missing_last,
            "valid_last": valid_last,
            "stale_last_pct": _percent(stale_last, len(obs_list)),
            "mean_last_age_seconds": _stats(last_ages),
            "median_last_age_seconds": _stats(last_ages).get("median") if last_ages else None,
            "max_last_age_seconds": max(last_ages) if last_ages else None,
            "p95_last_age_seconds": _stats(last_ages).get("p95") if last_ages else None,
            "stale_bid": stale_bid,
            "stale_ask": stale_ask,
            "last_timestamp_advanced": last_ts_advanced,
            "last_price_changed": last_price_changed,
            "volume_changed": volume_changed,
            "max_consecutive_stale_last": max_consec_stale,
            "bid_ask_fresh_when_last_stale": bid_ask_fresh_when_last_stale,
            "price_changed_when_last_ts_unchanged": price_changed_when_last_ts_not,
        }

    total_observations = len(observations)
    total_stale_last = sum(o.last_timestamp_status == "stale" for o in observations)
    total_valid_last = sum(o.last_timestamp_status == "valid" for o in observations)
    total_missing_last = sum(o.last_timestamp_status == "missing_timestamp" for o in observations)
    total_future_last = sum(o.last_timestamp_status == "future_timestamp" for o in observations)
    total_stale_bid = sum(o.bid_timestamp_status == "stale" for o in observations)
    total_stale_ask = sum(o.ask_timestamp_status == "stale" for o in observations)

    http_latencies = [o.http_latency_ms for o in observations if o.http_latency_ms is not None]
    http_statuses = defaultdict(int)
    for o in observations:
        http_statuses[str(o.http_status)] += 1

    return {
        "run_status": "interrupted" if interrupted else "success",
        "total_sweeps": sweep_count,
        "total_observations": total_observations,
        "symbols": symbols,
        "interval_seconds": interval_seconds,
        "duration_seconds": duration_seconds,
        "elapsed_seconds": elapsed,
        "requests_per_minute": requests_per_minute,
        "max_age_seconds": max_age,
        "clock_skew_tolerance_seconds": clock_skew_tolerance,
        "max_symbols_per_batch": max_symbols_per_batch,
        "http_latency_ms": _stats(http_latencies),
        "http_status_distribution": dict(http_statuses),
        "stale_last_total": total_stale_last,
        "valid_last_total": total_valid_last,
        "missing_last_total": total_missing_last,
        "future_last_total": total_future_last,
        "stale_last_pct": _percent(total_stale_last, total_observations),
        "stale_bid_total": total_stale_bid,
        "stale_ask_total": total_stale_ask,
        "stale_last_with_fresh_bid_ask": sum(
            1 for o in observations
            if o.last_timestamp_status == "stale"
            and o.bid_timestamp_status == "valid"
            and o.ask_timestamp_status == "valid"
        ),
        "price_changed_when_last_ts_unchanged": sum(
            1 for o in observations
            if not o.last_timestamp_changed and o.price_changed
        ),
        "per_symbol": per_symbol,
    }


# For backwards compatibility
def validate(symbols):
    return validate_symbols(symbols)
