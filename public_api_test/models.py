# Public.com API Tester - Data Models

import os
import time
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dataclasses import dataclass, field
import requests


@dataclass
class QuoteResult:
    """Result from a single quote request."""
    symbol: str
    http_status: int
    request_duration_ms: float
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    provider_timestamp: Optional[float] = None
    delayed_indicator: Optional[bool] = None
    raw_response: Optional[Dict] = None
    response_headers: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class LatencyObservation:
    """Single latency/freshness observation."""
    symbol: str
    request_start_timestamp_utc: str
    response_timestamp_utc: str
    http_latency_ms: float
    provider_timestamp: Optional[float] = None
    data_age_seconds: Optional[float] = None
    price: Optional[float] = None
    price_changed: bool = False
    unique_price_observations: int = 0

    def to_jsonl(self) -> str:
        """Convert to JSONL format for raw observations."""
        data = {
            "symbol": self.symbol,
            "request_start_timestamp_utc": self.request_start_timestamp_utc,
            "response_timestamp_utc": self.response_timestamp_utc,
            "http_latency_ms": self.http_latency_ms,
            "provider_timestamp": self.provider_timestamp,
            "data_age_seconds": self.data_age_seconds,
            "price": self.price,
            "price_changed": self.price_changed,
        }
        return json.dumps(data)


@dataclass
class RateLimitResult:
    """Rate limit probe results."""
    successful_requests: int
    rate_limit_429_count: int
    retry_after_headers: List[str]
    requests_per_minute: float
    rate_limit_scope: str = "unknown"
    raw_headers: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "successful_requests": self.successful_requests,
            "rate_limit_429_count": self.rate_limit_429_count,
            "retry_after_headers": self.retry_after_headers,
            "requests_per_minute": self.requests_per_minute,
            "rate_limit_scope": self.rate_limit_scope,
            "raw_headers": dict(self.raw_headers),
        }


@dataclass
class BatchQuoteResult:
    """Result from a batch quote request."""
    batch_size: int
    symbols_requested: List[str]
    http_latency_ms: float
    returned_symbols: int
    partial_failures: int
    completeness_ratio: float
    returned_data: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "batch_size": self.batch_size,
            "symbols_requested": self.symbols_requested,
            "http_latency_ms": self.http_latency_ms,
            "returned_symbols": self.returned_symbols,
            "partial_failures": self.partial_failures,
            "completeness_ratio": self.completeness_ratio,
        }


@dataclass
class HistoricalResult:
    """Result from a historical data request."""
    symbol: str
    interval: str
    http_latency_ms: float
    candles_returned: int
    maximum_available: bool
    missing_candles: int = 0
    extended_hours_included: bool = False
    timestamp_consistency: bool = True
    raw_response: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "http_latency_ms": self.http_latency_ms,
            "candles_returned": self.candles_returned,
            "maximum_available": self.maximum_available,
            "missing_candles": self.missing_candles,
            "extended_hours_included": self.extended_hours_included,
            "timestamp_consistency": self.timestamp_consistency,
        }