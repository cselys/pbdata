"""Offline regression tests for cross-validation module.

All tests use mocked responses — no network I/O, no live API calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from public_api_test.cross_validation.adapters import (
    PublicComAdapter,
    SchwabAdapter,
    WebullAdapter,
    _parse_ts,
    _safe_float,
    _ts_status,
)
from public_api_test.cross_validation.collector import CrossValidationCollector
from public_api_test.cross_validation.comparator import (
    compare_symbols,
    build_summary,
    _match_by_time_window,
    _match_triplets,
    _provider_metrics,
)
from public_api_test.cross_validation.models import (
    CrossValidationSummary,
    ProviderObservation,
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _recent_iso(offset_seconds: float = -5.0) -> str:
    """Generate an ISO 8601 timestamp close to now so it classifies as valid."""
    ts = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return ts.isoformat()


def _make_obs(
    provider: str,
    symbol: str,
    sweep: int,
    request_start: str,
    request_finished: str,
    http_status: int = 200,
    last_price: float | None = None,
    last_ts_raw: str | None = None,
    last_ts_utc: float | None = None,
    last_age: float | None = None,
    last_ts_status: str = "valid",
    bid_price: float | None = None,
    ask_price: float | None = None,
    volume: float | None = None,
    category: str = "success",
    quote_ts_raw: str | None = None,
    quote_ts_utc: float | None = None,
    quote_age: float | None = None,
    quote_ts_status: str | None = None,
) -> ProviderObservation:
    return ProviderObservation(
        provider=provider,
        symbol=symbol,
        sweep=sweep,
        scheduled_offset_seconds=0.0,
        actual_start_offset_seconds=0.0,
        request_start_utc=request_start,
        request_finished_utc=request_finished,
        http_status=http_status,
        http_latency_ms=50.0,
        category=category,
        api_error_code=None,
        last_price=last_price,
        last_timestamp_raw=last_ts_raw,
        last_timestamp_utc=last_ts_utc,
        last_age_seconds=last_age,
        last_timestamp_status=last_ts_status,
        bid_price=bid_price,
        bid_timestamp_raw=None,
        bid_timestamp_utc=None,
        bid_age_seconds=None,
        bid_timestamp_status=None,
        ask_price=ask_price,
        ask_timestamp_raw=None,
        ask_timestamp_utc=None,
        ask_age_seconds=None,
        ask_timestamp_status=None,
        volume=volume,
        quote_timestamp_raw=quote_ts_raw,
        quote_timestamp_utc=quote_ts_utc,
        quote_age_seconds=quote_age,
        quote_timestamp_status=quote_ts_status,
        error_category=None,
        raw_response=None,
    )


# ---------------------------------------------------------------------------
# Timestamp parsing tests
# ---------------------------------------------------------------------------


class TestTimestampParsing:
    def test_iso_string_with_z(self):
        assert _parse_ts("2026-10-20T14:00:00Z") is not None

    def test_iso_string_without_z(self):
        assert _parse_ts("2026-10-20T14:00:00+00:00") is not None

    def test_unix_seconds(self):
        assert _parse_ts(1789932000.0) == 1789932000.0

    def test_unix_milliseconds(self):
        result = _parse_ts(1789932000000)
        assert result is not None
        assert abs(result - 1789932000.0) < 1.0

    def test_none_returns_none(self):
        assert _parse_ts(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_ts("") is None

    def test_invalid_string_returns_none(self):
        assert _parse_ts("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# _ts_status tests
# ---------------------------------------------------------------------------


class TestTimestampStatus:
    def test_missing_timestamp(self):
        assert _ts_status(None, None) == "missing_timestamp"
        assert _ts_status("", None) == "missing_timestamp"

    def test_invalid_timestamp(self):
        assert _ts_status("2026-10-20T14:00:00Z", None) == "invalid_timestamp"

    def test_future_timestamp(self):
        assert _ts_status("2026-10-20T14:00:00Z", -5.0) == "future_timestamp"

    def test_stale_timestamp(self):
        assert _ts_status("2026-10-20T14:00:00Z", 120.0) == "stale"

    def test_valid_timestamp(self):
        assert _ts_status("2026-10-20T14:00:00Z", 5.0) == "valid"


# ---------------------------------------------------------------------------
# PublicComAdapter tests
# ---------------------------------------------------------------------------


class TestPublicComAdapter:
    def _make_client(self, batch_response):
        client = Mock()
        client.get_quotes_batch = Mock(return_value=batch_response)
        return client

    def test_valid_timestamps_extracted(self):
        recent = _recent_iso(-5.0)
        batch = {
            "category": "success",
            "http_status": 200,
            "http_latency_ms": 50.0,
            "instrument_results": [
                {
                    "symbol": "AAPL",
                    "category": "success",
                    "price": "123.45",
                    "provider_timestamp_raw": recent,
                    "bid_timestamp_raw": recent,
                    "ask_timestamp_raw": recent,
                    "bid_price": "123.40",
                    "ask_price": "123.50",
                    "volume": "1000",
                }
            ],
            "returned_symbol_list": ["AAPL"],
            "missing_symbols": [],
        }
        client = self._make_client(batch)
        adapter = PublicComAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.last_timestamp_raw == recent
        assert obs.last_price == 123.45
        assert obs.bid_price == 123.40
        assert obs.ask_price == 123.50
        assert obs.volume == 1000.0
        assert obs.provider == "public_com"
        assert obs.symbol == "AAPL"
        assert obs.last_timestamp_status == "valid"

    def test_missing_timestamps(self):
        batch = {
            "category": "success",
            "http_status": 200,
            "http_latency_ms": 50.0,
            "instrument_results": [
                {
                    "symbol": "AAPL",
                    "category": "success",
                    "price": "123.45",
                    # No timestamp fields
                    "bid_price": None,
                    "ask_price": None,
                    "volume": None,
                }
            ],
            "returned_symbol_list": ["AAPL"],
            "missing_symbols": [],
        }
        client = self._make_client(batch)
        adapter = PublicComAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.last_timestamp_raw is None
        assert obs.last_timestamp_status == "missing_timestamp"
        assert obs.last_age_seconds is None

    def test_invalid_timestamp_string(self):
        batch = {
            "category": "success",
            "http_status": 200,
            "http_latency_ms": 50.0,
            "instrument_results": [
                {
                    "symbol": "AAPL",
                    "category": "success",
                    "price": "123.45",
                    "provider_timestamp_raw": "not-a-timestamp",
                }
            ],
            "returned_symbol_list": ["AAPL"],
            "missing_symbols": [],
        }
        client = self._make_client(batch)
        adapter = PublicComAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        obs = obs_list[0]
        assert obs.last_timestamp_raw == "not-a-timestamp"
        assert obs.last_timestamp_status == "invalid_timestamp"

    def test_client_error(self):
        client = Mock()
        client.get_quotes_batch = Mock(side_effect=Exception("Network down"))
        adapter = PublicComAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        assert obs_list[0].category == "client_error"
        assert obs_list[0].error_category == "client_error"
        assert obs_list[0].last_timestamp_status == "missing_timestamp"

    def test_multiple_symbols(self):
        recent = _recent_iso(-5.0)
        batch = {
            "category": "success",
            "http_status": 200,
            "http_latency_ms": 50.0,
            "instrument_results": [
                {
                    "symbol": "AAPL",
                    "category": "success",
                    "price": "123.45",
                    "provider_timestamp_raw": recent,
                },
                {
                    "symbol": "NVDA",
                    "category": "success",
                    "price": "219.00",
                    "provider_timestamp_raw": recent,
                },
            ],
            "returned_symbol_list": ["AAPL", "NVDA"],
            "missing_symbols": [],
        }
        client = self._make_client(batch)
        adapter = PublicComAdapter(client)
        obs_list = adapter.poll_once(["AAPL", "NVDA"])
        assert len(obs_list) == 2
        symbols = {o.symbol for o in obs_list}
        assert symbols == {"AAPL", "NVDA"}


# ---------------------------------------------------------------------------
# SchwabAdapter tests
# ---------------------------------------------------------------------------


class TestSchwabAdapter:
    def _make_quote(self, symbol, price, trade_time_str, bid=None, ask=None, volume=None, quality="valid"):
        ts = datetime.fromisoformat(trade_time_str.replace("Z", "+00:00")) if trade_time_str else None
        q = Mock()
        q.last_price = price
        q.timestamp = ts
        q.bid = bid
        q.ask = ask
        q.volume = volume
        q.quality = quality
        q.day_high = None
        q.day_low = None
        q.day_open = None
        q.previous_close = None
        return q

    def _recent_ts(self):
        return _recent_iso(-5.0)

    def test_valid_quote(self):
        provider = Mock()
        recent = self._recent_ts()
        provider.get_quotes = Mock(return_value={
            "AAPL": self._make_quote("AAPL", 123.45, recent, bid=123.40, ask=123.50, volume=1000)
        })
        adapter = SchwabAdapter(provider)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.provider == "schwab"
        assert obs.last_price == 123.45
        assert obs.last_timestamp_raw is not None
        assert obs.last_timestamp_status == "valid"
        assert obs.bid_price == 123.40
        assert obs.ask_price == 123.50
        assert obs.volume == 1000.0
        assert obs.realtime is True

    def test_missing_symbol(self):
        provider = Mock()
        provider.get_quotes = Mock(return_value={})
        adapter = SchwabAdapter(provider)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        assert obs_list[0].last_timestamp_status == "missing_timestamp"
        assert obs_list[0].error_category == "missing_symbol"

    def test_stale_quality(self):
        provider = Mock()
        recent = self._recent_ts()
        provider.get_quotes = Mock(return_value={
            "AAPL": self._make_quote("AAPL", 123.45, recent, quality="stale")
        })
        adapter = SchwabAdapter(provider)
        obs_list = adapter.poll_once(["AAPL"])
        obs = obs_list[0]
        assert obs.realtime is False


# ---------------------------------------------------------------------------
# WebullAdapter tests
# ---------------------------------------------------------------------------


class TestWebullAdapter:
    def test_valid_snapshot(self):
        client = Mock()
        recent = _recent_iso(-5.0)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": recent,
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.provider == "webull"
        assert obs.last_price == 123.45
        assert obs.last_timestamp_raw == recent
        assert obs.last_timestamp_status == "valid"
        assert obs.bid_price == 123.40
        assert obs.ask_price == 123.50
        assert obs.volume == 1000000.0
        assert obs.trade_status == "RTH"

    def test_quote_timestamp_present_and_valid(self):
        client = Mock()
        recent_lt = _recent_iso(-5.0)
        recent_qt = _recent_iso(-2.0)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": recent_lt,
                "quote_time": recent_qt,
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.quote_timestamp_raw == recent_qt
        assert obs.quote_timestamp_utc is not None
        assert obs.quote_age_seconds is not None
        assert obs.quote_timestamp_status == "valid"
        # Quote timestamp is distinct from last-trade timestamp
        assert obs.quote_timestamp_raw != obs.last_timestamp_raw

    def test_quote_timestamp_missing(self):
        client = Mock()
        recent_lt = _recent_iso(-5.0)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": recent_lt,
                # quote_time intentionally absent
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.quote_timestamp_raw is None
        assert obs.quote_timestamp_utc is None
        assert obs.quote_age_seconds is None
        assert obs.quote_timestamp_status == "missing_timestamp"
        # Last-trade fields are unaffected
        assert obs.last_timestamp_raw == recent_lt
        assert obs.last_timestamp_status == "valid"

    def test_quote_timestamp_invalid(self):
        client = Mock()
        recent_lt = _recent_iso(-5.0)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": recent_lt,
                "quote_time": "not-a-timestamp",
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.quote_timestamp_raw == "not-a-timestamp"
        assert obs.quote_timestamp_utc is None
        assert obs.quote_age_seconds is None
        assert obs.quote_timestamp_status == "invalid_timestamp"
        # Last-trade fields are unaffected
        assert obs.last_timestamp_raw == recent_lt
        assert obs.last_timestamp_status == "valid"

    def test_quote_timestamp_unix_milliseconds(self):
        client = Mock()
        recent_lt = _recent_iso(-5.0)
        # quote_time as Unix milliseconds (as seen in live data)
        recent_qt_ms = int((datetime.now(timezone.utc) + timedelta(seconds=-2.0)).timestamp() * 1000)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": recent_lt,
                "quote_time": recent_qt_ms,
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.quote_timestamp_raw == recent_qt_ms
        assert obs.quote_timestamp_utc is not None
        assert obs.quote_age_seconds is not None
        assert abs(obs.quote_age_seconds - 2.0) < 2.0  # approximately 2 seconds old
        assert obs.quote_timestamp_status == "valid"

    def test_quote_timestamp_distinct_from_last_trade(self):
        client = Mock()
        lt = _recent_iso(-10.0)
        qt = _recent_iso(-2.0)
        client.snapshots = Mock(return_value={
            "AAPL": {
                "symbol": "AAPL",
                "price": 123.45,
                "last_trade_time": lt,
                "quote_time": qt,
                "bid": 123.40,
                "ask": 123.50,
                "volume": 1000000,
                "trade_status": "RTH",
            }
        })
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        obs = obs_list[0]
        assert obs.last_timestamp_raw == lt
        assert obs.quote_timestamp_raw == qt
        assert obs.last_timestamp_raw != obs.quote_timestamp_raw
        assert obs.last_timestamp_status == "valid"
        assert obs.quote_timestamp_status == "valid"

    def test_missing_symbol(self):
        client = Mock()
        client.snapshots = Mock(return_value={})
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        assert len(obs_list) == 1
        assert obs_list[0].last_timestamp_status == "missing_timestamp"
        assert obs_list[0].error_category == "missing_symbol"

    def test_client_error(self):
        client = Mock()
        client.snapshots = Mock(side_effect=Exception("Network error"))
        adapter = WebullAdapter(client)
        obs_list = adapter.poll_once(["AAPL"])
        assert len(obs_list) == 1
        assert obs_list[0].category == "client_error"


# ---------------------------------------------------------------------------
# Collector tests
# ---------------------------------------------------------------------------


class TestCollector:
    def test_collector_writes_jsonl(self, tmp_path, monkeypatch):
        from public_api_test import config
        monkeypatch.setattr(config, "RAW_OBSERVATIONS_DIR", tmp_path / "raw")
        monkeypatch.setattr(config, "SUMMARY_STATS_DIR", tmp_path / "summaries")
        (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
        (tmp_path / "summaries").mkdir(parents=True, exist_ok=True)

        adapter = Mock()
        recent_ts = _recent_iso(-5.0)
        adapter.poll_once = Mock(return_value=[
            _make_obs(
                provider="public_com",
                symbol="AAPL",
                sweep=1,
                request_start=_recent_iso(-1.0),
                request_finished=_recent_iso(0.0),
                last_price=123.45,
                last_ts_raw=recent_ts,
                last_ts_utc=None,
                last_age=5.1,
                last_ts_status="valid",
                bid_price=123.40,
                ask_price=123.50,
                volume=1000.0,
            )
        ])

        collector = CrossValidationCollector(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=1,
            public_adapter=adapter,
        )

        collector._sweep_count = 0
        collector._poll_provider(adapter, "public_com", 1, 0.0, 0.0)

        raw_path = tmp_path / "raw" / "test.jsonl"
        with raw_path.open("w", encoding="utf-8") as stream:
            for obs in collector._observations:
                stream.write(obs.to_jsonl() + "\n")

        lines = raw_path.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["provider"] == "public_com"
        assert record["symbol"] == "AAPL"
        assert record["last_price"] == 123.45
        assert record["last_timestamp_raw"] == recent_ts
        assert record["last_timestamp_status"] == "valid"

    def test_collector_requires_adapter(self):
        with pytest.raises(ValueError):
            CrossValidationCollector(
                symbols=["AAPL"],
                interval_seconds=10,
                duration_seconds=60,
            )

    def test_collector_requires_symbols(self):
        with pytest.raises(ValueError):
            CrossValidationCollector(
                symbols=[],
                interval_seconds=10,
                duration_seconds=60,
                public_adapter=Mock(),
            )


# ---------------------------------------------------------------------------
# Comparator tests
# ---------------------------------------------------------------------------


class TestComparator:
    def test_match_by_time_window(self):
        now = _recent_iso(0.0)
        obs_a = [
            _make_obs("public_com", "AAPL", 1, "t1", now),
            _make_obs("public_com", "AAPL", 2, "t2", _recent_iso(10.0)),
        ]
        obs_b = [
            _make_obs("schwab", "AAPL", 1, "t1", _recent_iso(0.5)),
            _make_obs("schwab", "AAPL", 2, "t2", _recent_iso(10.5)),
        ]
        matches = _match_by_time_window(obs_a, obs_b, tolerance_seconds=1.0)
        assert len(matches) == 2

    def test_match_triplets(self):
        now = _recent_iso(0.0)
        public_obs = [
            _make_obs("public_com", "AAPL", 1, "t1", now, last_price=100.0),
        ]
        schwab_obs = [
            _make_obs("schwab", "AAPL", 1, "t1", _recent_iso(0.2), last_price=100.10),
        ]
        webull_obs = [
            _make_obs("webull", "AAPL", 1, "t1", _recent_iso(0.4), last_price=99.95),
        ]
        triplets = _match_triplets(public_obs, schwab_obs, webull_obs, tolerance_seconds=1.0)
        assert len(triplets) == 1

    def test_provider_metrics(self):
        now = _recent_iso(0.0)
        recent_ts = _recent_iso(-5.0)
        obs_list = [
            _make_obs("public_com", "AAPL", 1, "t1", now,
                      last_price=100.0, last_ts_raw=recent_ts, last_ts_status="valid"),
            _make_obs("public_com", "AAPL", 2, "t2", _recent_iso(10.0),
                      last_price=101.0, last_ts_raw=recent_ts, last_ts_status="valid"),
            _make_obs("public_com", "AAPL", 3, "t3", _recent_iso(20.0),
                      last_price=101.0, last_ts_raw=None, last_ts_status="missing_timestamp"),
        ]
        metrics = _provider_metrics(obs_list)
        assert metrics["observations"] == 3
        assert metrics["price_changes"] == 1
        assert metrics["price_changed_ts_unchanged"] == 1
        assert metrics["missing_timestamps"] == 1
        assert metrics["valid_timestamps"] == 2

    def test_compare_symbols(self):
        now = _recent_iso(0.0)
        recent_ts = _recent_iso(-5.0)
        public_obs = [
            _make_obs("public_com", "AAPL", 1, "t1", now,
                      last_price=100.0, last_ts_raw=recent_ts, last_ts_status="valid"),
            _make_obs("public_com", "AAPL", 2, "t2", _recent_iso(10.0),
                      last_price=101.0, last_ts_raw=recent_ts, last_ts_status="valid"),
        ]
        schwab_obs = [
            _make_obs("schwab", "AAPL", 1, "t1", _recent_iso(0.2),
                      last_price=100.05, last_ts_raw=recent_ts, last_ts_status="valid"),
        ]
        webull_obs = [
            _make_obs("webull", "AAPL", 1, "t1", _recent_iso(0.4),
                      last_price=99.98, last_ts_raw=recent_ts, last_ts_status="valid"),
        ]

        comparison = compare_symbols(public_obs, schwab_obs, webull_obs, "AAPL", tolerance_seconds=1.0)
        assert comparison.symbol == "AAPL"
        assert comparison.public_com_count == 2
        assert comparison.schwab_count == 1
        assert comparison.webull_count == 1
        assert comparison.matched_triplets == 1
        assert comparison.unmatched_public_com == 1
        assert comparison.matched_pairs >= 1

    def test_anomaly_detection(self):
        now = _recent_iso(0.0)
        recent_ts = _recent_iso(-5.0)
        public_obs = [
            _make_obs("public_com", "BLK", 1, "t1", now,
                      last_price=340.0, last_ts_raw=recent_ts, last_ts_status="valid"),
            _make_obs("public_com", "BLK", 2, "t2", _recent_iso(10.0),
                      last_price=340.50, last_ts_raw=recent_ts, last_ts_status="valid"),
        ]
        schwab_obs = []
        webull_obs = []

        comparison = compare_symbols(public_obs, schwab_obs, webull_obs, "BLK", tolerance_seconds=1.0)
        assert len(comparison.anomalies) >= 1
        anomaly = comparison.anomalies[0]
        assert anomaly["type"] == "price_changed_ts_unchanged"
        assert anomaly["symbol"] == "BLK"
        assert anomaly["provider"] == "public_com"

    def test_build_summary(self):
        now = _recent_iso(0.0)
        recent_ts = _recent_iso(-5.0)
        comp1 = compare_symbols(
            [_make_obs("public_com", "AAPL", 1, "t1", now,
                       last_price=100.0, last_ts_raw=recent_ts, last_ts_status="valid")],
            [],
            [],
            "AAPL",
            tolerance_seconds=1.0,
        )
        comp2 = compare_symbols(
            [_make_obs("public_com", "NVDA", 1, "t1", now,
                       last_price=200.0, last_ts_raw=None, last_ts_status="missing_timestamp")],
            [],
            [],
            "NVDA",
            tolerance_seconds=1.0,
        )

        summary = build_summary(
            run_id="test_run_20261020",
            symbols=["AAPL", "NVDA"],
            providers=["public_com"],
            interval_seconds=10,
            duration_seconds=900,
            comparisons={"AAPL": comp1, "NVDA": comp2},
            market_session="regular",
            tolerance_seconds=1.0,
        )

        assert summary.run_id == "test_run_20261020"
        assert summary.symbols == ["AAPL", "NVDA"]
        assert summary.providers == ["public_com"]
        assert summary.total_anomalies == 1  # NVDA has missing timestamp anomaly

    def test_summary_to_dict(self):
        now = _recent_iso(0.0)
        recent_ts = _recent_iso(-5.0)
        comp = compare_symbols(
            [_make_obs("public_com", "AAPL", 1, "t1", now,
                       last_price=100.0, last_ts_raw=recent_ts, last_ts_status="valid")],
            [],
            [],
            "AAPL",
            tolerance_seconds=1.0,
        )
        summary = build_summary(
            run_id="test_run",
            symbols=["AAPL"],
            providers=["public_com"],
            interval_seconds=10,
            duration_seconds=900,
            comparisons={"AAPL": comp},
            market_session="regular",
            tolerance_seconds=1.0,
        )
        d = summary.to_dict()
        assert d["run_id"] == "test_run"
        assert d["symbols"] == ["AAPL"]
        assert "comparisons" in d
        assert "AAPL" in d["comparisons"]
