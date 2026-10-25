"""Provider adapters for cross-validation.

Each adapter wraps an existing provider client and converts its response
into a standardized ProviderObservation. Adapters do not modify existing
client source files; they only consume them.

All adapters implement the same interface:
    poll_once(symbols) -> list[ProviderObservation]

For offline testing, construct an adapter with a mock client object that
implements the same method signature as the real client.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from public_api_test.cross_validation.models import ProviderObservation


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[float]:
    """Parse a timestamp into Unix epoch seconds.

    Handles ISO 8601 strings (with or without Z), Unix seconds, and Unix
    milliseconds (> 1e11). Returns None for invalid/unparseable values.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic: values > 1e11 are milliseconds
        if v > 1e11:
            v = v / 1000.0
        return v
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


def _ts_status(raw_val: Optional[str], age_sec: Optional[float], max_age: float = 60.0) -> str:
    """Classify a timestamp as valid / stale / future_timestamp / missing_timestamp / invalid_timestamp."""
    if raw_val is None or raw_val == "":
        return "missing_timestamp"
    if age_sec is None:
        return "invalid_timestamp"
    if age_sec < 0:
        return "future_timestamp"
    if age_sec > max_age:
        return "stale"
    return "valid"


def _compute_age(received_dt: datetime, provider_ts: Optional[float]) -> Optional[float]:
    if provider_ts is None:
        return None
    return received_dt.timestamp() - provider_ts


# ---------------------------------------------------------------------------
# Public.com adapter
# ---------------------------------------------------------------------------


class PublicComAdapter:
    """Wrap PublicComClient.get_quotes_batch and convert to ProviderObservation.

    Reuses the existing PublicComClient without modifying it.
    """

    def __init__(self, client):
        self.client = client

    def poll_once(self, symbols: list[str]) -> list[ProviderObservation]:
        """Poll Public.com once for all symbols in a single batch."""
        req_start = _now_utc_iso()
        req_start_dt = datetime.now(timezone.utc)
        sent = time.monotonic()

        try:
            batch = self.client.get_quotes_batch(symbols)
        except Exception as exc:  # pragma: no cover - network failures
            return [
                ProviderObservation(
                    provider="public_com",
                    symbol=s,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=_now_utc_iso(),
                    http_status=None,
                    http_latency_ms=None,
                    category="client_error",
                    api_error_code=type(exc).__name__,
                    last_price=None,
                    last_timestamp_raw=None,
                    last_timestamp_utc=None,
                    last_age_seconds=None,
                    last_timestamp_status="missing_timestamp",
                    bid_price=None,
                    bid_timestamp_raw=None,
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=None,
                    ask_timestamp_raw=None,
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=None,
                    error_category="client_error",
                    raw_response=None,
                )
                for s in symbols
            ]

        req_finished = _now_utc_iso()
        req_finished_dt = datetime.now(timezone.utc)
        latency_ms = None
        try:
            latency_ms = (req_finished_dt - req_start_dt).total_seconds() * 1000.0
        except Exception:
            pass

        http_status = batch.get("http_status")
        category = batch.get("category", "unknown")
        api_error_code = batch.get("api_error_code")

        instrument_results = batch.get("instrument_results", [])
        instrument_map = {
            r.get("symbol"): r
            for r in instrument_results
            if isinstance(r, dict)
        }

        observations: list[ProviderObservation] = []
        for symbol in symbols:
            instr = instrument_map.get(symbol, {})
            raw_response = instr.get("raw_response", {})

            last_price = _safe_float(instr.get("price"))
            bid_price = _safe_float(instr.get("bid_price")) or _safe_float(
                raw_response.get("bid")
            )
            ask_price = _safe_float(instr.get("ask_price")) or _safe_float(
                raw_response.get("ask")
            )

            last_ts_raw = (
                instr.get("provider_timestamp_raw")
                or instr.get("lastTimestamp")
                or instr.get("last_timestamp")
                or raw_response.get("lastTimestamp")
                or raw_response.get("last_timestamp")
            )
            bid_ts_raw = (
                instr.get("bid_timestamp_raw")
                or instr.get("bidTimestamp")
                or instr.get("bid_timestamp")
                or raw_response.get("bidTimestamp")
            )
            ask_ts_raw = (
                instr.get("ask_timestamp_raw")
                or instr.get("askTimestamp")
                or instr.get("ask_timestamp")
                or raw_response.get("askTimestamp")
            )

            volume = _safe_float(instr.get("volume")) or _safe_float(
                raw_response.get("volume")
            )

            last_ts_utc = _parse_ts(last_ts_raw)
            bid_ts_utc = _parse_ts(bid_ts_raw)
            ask_ts_utc = _parse_ts(ask_ts_raw)

            last_age_sec = _compute_age(req_finished_dt, last_ts_utc)
            bid_age_sec = _compute_age(req_finished_dt, bid_ts_utc)
            ask_age_sec = _compute_age(req_finished_dt, ask_ts_utc)

            observations.append(
                ProviderObservation(
                    provider="public_com",
                    symbol=symbol,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=req_finished,
                    http_status=http_status,
                    http_latency_ms=latency_ms,
                    category=category,
                    api_error_code=api_error_code,
                    last_price=last_price,
                    last_timestamp_raw=last_ts_raw,
                    last_timestamp_utc=last_ts_utc,
                    last_age_seconds=last_age_sec,
                    last_timestamp_status=_ts_status(last_ts_raw, last_age_sec),
                    bid_price=bid_price,
                    bid_timestamp_raw=bid_ts_raw,
                    bid_timestamp_utc=bid_ts_utc,
                    bid_age_seconds=bid_age_sec,
                    bid_timestamp_status=_ts_status(bid_ts_raw, bid_age_sec),
                    ask_price=ask_price,
                    ask_timestamp_raw=ask_ts_raw,
                    ask_timestamp_utc=ask_ts_utc,
                    ask_age_seconds=ask_age_sec,
                    ask_timestamp_status=_ts_status(ask_ts_raw, ask_age_sec),
                    volume=volume,
                    error_category=None if http_status == 200 else "api_error",
                    raw_response=self._sanitize_raw(raw_response),
                )
            )

        return observations

    @staticmethod
    def _sanitize_raw(raw: Any) -> Optional[dict]:
        """Return a shallow copy of raw_response without credential-like fields."""
        if not isinstance(raw, dict):
            return None
        result = {}
        for k, v in raw.items():
            kl = k.lower()
            if any(t in kl for t in ("token", "secret", "authorization", "apikey", "password")):
                result[k] = "[REDACTED]"
            else:
                result[k] = v
        return result


# ---------------------------------------------------------------------------
# Schwab adapter
# ---------------------------------------------------------------------------


class SchwabAdapter:
    """Wrap SchwabStockProvider.get_quotes and convert to ProviderObservation.

    Reuses the existing SchwabStockProvider from the realtimesignals project
    without modifying it. The provider project must be on sys.path.
    """

    def __init__(self, provider):
        self.provider = provider

    def poll_once(self, symbols: list[str]) -> list[ProviderObservation]:
        """Poll Schwab once for all symbols."""
        req_start = _now_utc_iso()
        req_start_dt = datetime.now(timezone.utc)
        sent = time.monotonic()

        try:
            quotes = self.provider.get_quotes(symbols)
        except Exception as exc:  # pragma: no cover - network failures
            return [
                ProviderObservation(
                    provider="schwab",
                    symbol=s,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=_now_utc_iso(),
                    http_status=None,
                    http_latency_ms=None,
                    category="client_error",
                    api_error_code=type(exc).__name__,
                    last_price=None,
                    last_timestamp_raw=None,
                    last_timestamp_utc=None,
                    last_age_seconds=None,
                    last_timestamp_status="missing_timestamp",
                    bid_price=None,
                    bid_timestamp_raw=None,
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=None,
                    ask_timestamp_raw=None,
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=None,
                    realtime=None,
                    error_category="client_error",
                    raw_response=None,
                )
                for s in symbols
            ]

        req_finished = _now_utc_iso()
        req_finished_dt = datetime.now(timezone.utc)
        latency_ms = None
        try:
            latency_ms = (req_finished_dt - req_start_dt).total_seconds() * 1000.0
        except Exception:
            pass

        observations: list[ProviderObservation] = []
        for symbol in symbols:
            q = quotes.get(symbol)
            if q is None:
                observations.append(
                    ProviderObservation(
                        provider="schwab",
                        symbol=symbol,
                        sweep=0,
                        scheduled_offset_seconds=0.0,
                        actual_start_offset_seconds=0.0,
                        request_start_utc=req_start,
                        request_finished_utc=req_finished,
                        http_status=None,
                        http_latency_ms=latency_ms,
                        category="missing",
                        api_error_code=None,
                        last_price=None,
                        last_timestamp_raw=None,
                        last_timestamp_utc=None,
                        last_age_seconds=None,
                        last_timestamp_status="missing_timestamp",
                        bid_price=None,
                        bid_timestamp_raw=None,
                        bid_timestamp_utc=None,
                        bid_age_seconds=None,
                        bid_timestamp_status=None,
                        ask_price=None,
                        ask_timestamp_raw=None,
                        ask_timestamp_utc=None,
                        ask_age_seconds=None,
                        ask_timestamp_status=None,
                        volume=None,
                        realtime=None,
                        error_category="missing_symbol",
                        raw_response=None,
                    )
                )
                continue

            last_price = _safe_float(getattr(q, "last_price", None))
            bid_price = _safe_float(getattr(q, "bid", None))
            ask_price = _safe_float(getattr(q, "ask", None))
            volume = _safe_float(getattr(q, "volume", None))
            realtime = getattr(q, "quality", None) == "valid"
            quality = getattr(q, "quality", None)

            # Schwab Quote.timestamp is a timezone-aware datetime
            ts = getattr(q, "timestamp", None)
            last_ts_raw = ts.isoformat() if isinstance(ts, datetime) else None
            last_ts_utc = ts.timestamp() if isinstance(ts, datetime) else None

            last_age_sec = _compute_age(req_finished_dt, last_ts_utc)

            observations.append(
                ProviderObservation(
                    provider="schwab",
                    symbol=symbol,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=req_finished,
                    http_status=200 if q else None,
                    http_latency_ms=latency_ms,
                    category="success" if q else "missing",
                    api_error_code=None,
                    last_price=last_price,
                    last_timestamp_raw=last_ts_raw,
                    last_timestamp_utc=last_ts_utc,
                    last_age_seconds=last_age_sec,
                    last_timestamp_status=_ts_status(last_ts_raw, last_age_sec),
                    bid_price=bid_price,
                    bid_timestamp_raw=None,  # Schwab quote does not provide bid timestamp
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=ask_price,
                    ask_timestamp_raw=None,  # Schwab quote does not provide ask timestamp
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=volume,
                    realtime=realtime,
                    error_category=None if quality in ("valid", "stale") else "api_error",
                    raw_response={
                        "quality": quality,
                        "day_high": getattr(q, "day_high", None),
                        "day_low": getattr(q, "day_low", None),
                        "day_open": getattr(q, "day_open", None),
                        "previous_close": getattr(q, "previous_close", None),
                    },
                )
            )

        return observations


# ---------------------------------------------------------------------------
# Webull adapter
# ---------------------------------------------------------------------------


class WebullAdapter:
    """Wrap WebullClient.snapshots and convert to ProviderObservation.

    Reuses the existing WebullClient from the webullrealcheck project
    without modifying it. The provider project must be on sys.path.
    """

    def __init__(self, client):
        self.client = client

    def poll_once(self, symbols: list[str]) -> list[ProviderObservation]:
        """Poll Webull once for all symbols."""
        req_start = _now_utc_iso()
        req_start_dt = datetime.now(timezone.utc)
        sent = time.monotonic()

        try:
            snapshots = self.client.snapshots(symbols)
        except Exception as exc:  # pragma: no cover - network failures
            return [
                ProviderObservation(
                    provider="webull",
                    symbol=s,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=_now_utc_iso(),
                    http_status=None,
                    http_latency_ms=None,
                    category="client_error",
                    api_error_code=type(exc).__name__,
                    last_price=None,
                    last_timestamp_raw=None,
                    last_timestamp_utc=None,
                    last_age_seconds=None,
                    last_timestamp_status="missing_timestamp",
                    bid_price=None,
                    bid_timestamp_raw=None,
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=None,
                    ask_timestamp_raw=None,
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=None,
                    error_category="client_error",
                    raw_response=None,
                )
                for s in symbols
            ]

        req_finished = _now_utc_iso()
        req_finished_dt = datetime.now(timezone.utc)
        latency_ms = None
        try:
            latency_ms = (req_finished_dt - req_start_dt).total_seconds() * 1000.0
        except Exception:
            pass

        observations: list[ProviderObservation] = []
        for symbol in symbols:
            snap = snapshots.get(symbol)
            if not snap:
                observations.append(
                    ProviderObservation(
                        provider="webull",
                        symbol=symbol,
                        sweep=0,
                        scheduled_offset_seconds=0.0,
                        actual_start_offset_seconds=0.0,
                        request_start_utc=req_start,
                        request_finished_utc=req_finished,
                        http_status=None,
                        http_latency_ms=latency_ms,
                        category="missing",
                        api_error_code=None,
                        last_price=None,
                        last_timestamp_raw=None,
                        last_timestamp_utc=None,
                        last_age_seconds=None,
                        last_timestamp_status="missing_timestamp",
                        bid_price=None,
                        bid_timestamp_raw=None,
                        bid_timestamp_utc=None,
                        bid_age_seconds=None,
                        bid_timestamp_status=None,
                        ask_price=None,
                        ask_timestamp_raw=None,
                        ask_timestamp_utc=None,
                        ask_age_seconds=None,
                        ask_timestamp_status=None,
                        volume=None,
                        error_category="missing_symbol",
                        raw_response=None,
                    )
                )
                continue

            # Webull snapshot fields.
            # last_trade_time: timestamp of the last trade (Unix milliseconds)
            # quote_time: timestamp of the last quote update (Unix milliseconds)
            # These are distinct fields: quote_time can advance while
            # last_trade_time stays unchanged.
            last_price = _safe_float(snap.get("price"))
            last_ts_raw = snap.get("last_trade_time")
            last_ts_utc = _parse_ts(last_ts_raw)
            last_age_sec = _compute_age(req_finished_dt, last_ts_utc)

            quote_ts_raw = snap.get("quote_time")
            quote_ts_utc = _parse_ts(quote_ts_raw)
            quote_age_sec = _compute_age(req_finished_dt, quote_ts_utc)

            bid_price = _safe_float(snap.get("bid"))
            ask_price = _safe_float(snap.get("ask"))
            volume = _safe_float(snap.get("volume"))
            trade_status = snap.get("trade_status")

            observations.append(
                ProviderObservation(
                    provider="webull",
                    symbol=symbol,
                    sweep=0,
                    scheduled_offset_seconds=0.0,
                    actual_start_offset_seconds=0.0,
                    request_start_utc=req_start,
                    request_finished_utc=req_finished,
                    http_status=200,
                    http_latency_ms=latency_ms,
                    category="success",
                    api_error_code=None,
                    last_price=last_price,
                    last_timestamp_raw=last_ts_raw,
                    last_timestamp_utc=last_ts_utc,
                    last_age_seconds=last_age_sec,
                    last_timestamp_status=_ts_status(last_ts_raw, last_age_sec),
                    bid_price=bid_price,
                    bid_timestamp_raw=None,  # Webull snapshot may not include bid timestamp
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=ask_price,
                    ask_timestamp_raw=None,  # Webull snapshot may not include ask timestamp
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=volume,
                    quote_timestamp_raw=quote_ts_raw,
                    quote_timestamp_utc=quote_ts_utc,
                    quote_age_seconds=quote_age_sec,
                    quote_timestamp_status=_ts_status(quote_ts_raw, quote_age_sec),
                    trade_status=trade_status,
                    error_category=None,
                    raw_response=self._sanitize_raw(snap),
                )
            )

        return observations

    @staticmethod
    def _sanitize_raw(raw: Any) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        result = {}
        for k, v in raw.items():
            kl = k.lower()
            if any(t in kl for t in ("token", "secret", "authorization", "apikey", "password")):
                result[k] = "[REDACTED]"
            else:
                result[k] = v
        return result
