"""Cross-provider observation models.

Each observation records one provider's response for one symbol at one point in time.
Provider timestamps and observation/request times are kept distinct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class ProviderObservation:
    """A single symbol observation from one provider at one sweep."""

    provider: str  # "public_com" | "schwab" | "webull"
    symbol: str
    sweep: int
    scheduled_offset_seconds: float
    actual_start_offset_seconds: float
    request_start_utc: str  # ISO 8601 UTC, captured before request
    request_finished_utc: str  # ISO 8601 UTC, captured after response
    http_status: Optional[int]
    http_latency_ms: Optional[float]
    category: str  # provider-specific category (success, error, etc.)
    api_error_code: Optional[str]

    # Last price and its provider timestamp
    last_price: Optional[float]
    last_timestamp_raw: Optional[str]  # provider's original string
    last_timestamp_utc: Optional[float]  # normalized epoch seconds
    last_age_seconds: Optional[float]  # computed: request_finished - provider_ts
    last_timestamp_status: str  # valid | stale | future_timestamp | missing_timestamp | invalid_timestamp

    # Bid
    bid_price: Optional[float]
    bid_timestamp_raw: Optional[str]
    bid_timestamp_utc: Optional[float]
    bid_age_seconds: Optional[float]
    bid_timestamp_status: Optional[str]

    # Ask
    ask_price: Optional[float]
    ask_timestamp_raw: Optional[str]
    ask_timestamp_utc: Optional[float]
    ask_age_seconds: Optional[float]
    ask_timestamp_status: Optional[str]

    # Volume
    volume: Optional[float]

    # Quote/update timestamp (Webull only)
    # Distinct from last-trade timestamp: reflects when the quote (bid/ask)
    # was last updated, not when the last trade occurred.
    quote_timestamp_raw: Optional[str] = None  # provider's original string
    quote_timestamp_utc: Optional[float] = None  # normalized epoch seconds
    quote_age_seconds: Optional[float] = None  # computed: request_finished - provider_ts
    quote_timestamp_status: Optional[str] = None  # valid | stale | future_timestamp | missing_timestamp | invalid_timestamp

    # Provider-specific metadata
    realtime: Optional[bool] = None  # Schwab only
    trade_status: Optional[str] = None  # Webull only
    delay_minutes: Optional[float] = None  # Webull bars only

    # Raw response (safely redacted)
    raw_response: Optional[dict] = field(default=None, repr=False)

    # Error classification
    error_category: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "symbol": self.symbol,
            "sweep": self.sweep,
            "scheduled_offset_seconds": self.scheduled_offset_seconds,
            "actual_start_offset_seconds": self.actual_start_offset_seconds,
            "request_start_utc": self.request_start_utc,
            "request_finished_utc": self.request_finished_utc,
            "http_status": self.http_status,
            "http_latency_ms": self.http_latency_ms,
            "category": self.category,
            "api_error_code": self.api_error_code,
            "last_price": self.last_price,
            "last_timestamp_raw": self.last_timestamp_raw,
            "last_timestamp_utc": self.last_timestamp_utc,
            "last_age_seconds": self.last_age_seconds,
            "last_timestamp_status": self.last_timestamp_status,
            "bid_price": self.bid_price,
            "bid_timestamp_raw": self.bid_timestamp_raw,
            "bid_timestamp_utc": self.bid_timestamp_utc,
            "bid_age_seconds": self.bid_age_seconds,
            "bid_timestamp_status": self.bid_timestamp_status,
            "ask_price": self.ask_price,
            "ask_timestamp_raw": self.ask_timestamp_raw,
            "ask_timestamp_utc": self.ask_timestamp_utc,
            "ask_age_seconds": self.ask_age_seconds,
            "ask_timestamp_status": self.ask_timestamp_status,
            "volume": self.volume,
            "quote_timestamp_raw": self.quote_timestamp_raw,
            "quote_timestamp_utc": self.quote_timestamp_utc,
            "quote_age_seconds": self.quote_age_seconds,
            "quote_timestamp_status": self.quote_timestamp_status,
            "realtime": self.realtime,
            "trade_status": self.trade_status,
            "delay_minutes": self.delay_minutes,
            "raw_response": self.raw_response,
            "error_category": self.error_category,
        }

    def to_jsonl(self) -> str:
        import json
        return json.dumps(self.to_dict())


@dataclass
class CrossProviderComparison:
    """Result of aligning observations across providers for one symbol."""

    symbol: str
    matching_tolerance_seconds: float
    public_com_count: int
    schwab_count: int
    webull_count: int
    matched_triplets: int
    matched_pairs: int
    unmatched_public_com: int
    unmatched_schwab: int
    unmatched_webull: int

    # Per-symbol metrics per provider
    public_com_metrics: dict
    schwab_metrics: dict
    webull_metrics: dict

    # Cross-provider price differences (for temporally close observations)
    price_differences: list[dict]  # [{t1, t2, provider1, provider2, price1, price2, diff_pct}]

    # Anomalies detected (without designating ground truth)
    anomalies: list[dict]

    raw_references: dict[str, str]  # provider -> output file path

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "matching_tolerance_seconds": self.matching_tolerance_seconds,
            "public_com_count": self.public_com_count,
            "schwab_count": self.schwab_count,
            "webull_count": self.webull_count,
            "matched_triplets": self.matched_triplets,
            "matched_pairs": self.matched_pairs,
            "unmatched_public_com": self.unmatched_public_com,
            "unmatched_schwab": self.unmatched_schwab,
            "unmatched_webull": self.unmatched_webull,
            "public_com_metrics": self.public_com_metrics,
            "schwab_metrics": self.schwab_metrics,
            "webull_metrics": self.webull_metrics,
            "price_differences": self.price_differences,
            "anomalies": self.anomalies,
            "raw_references": self.raw_references,
        }


@dataclass
class CrossValidationSummary:
    """Overall cross-validation summary."""

    run_id: str
    symbols: list[str]
    providers: list[str]
    interval_seconds: int
    duration_seconds: int
    matching_tolerance_seconds: float
    market_session: Optional[str]
    comparisons: dict[str, CrossProviderComparison]
    total_anomalies: int
    raw_references: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "symbols": self.symbols,
            "providers": self.providers,
            "interval_seconds": self.interval_seconds,
            "duration_seconds": self.duration_seconds,
            "matching_tolerance_seconds": self.matching_tolerance_seconds,
            "market_session": self.market_session,
            "comparisons": {s: c.to_dict() for s, c in self.comparisons.items()},
            "total_anomalies": self.total_anomalies,
            "raw_references": self.raw_references,
        }
