"""Regression tests for the missing timestamp bug fix in quote_diagnostics."""

from datetime import datetime, timedelta
from unittest.mock import Mock
import json

import pytest

from public_api_test.client import PublicComClient
from public_api_test.quote_diagnostics import run_quote_diagnostics


class TestTimestampExtractionFix:
    """Regression tests for the missing timestamp bug in quote_diagnostics."""

    def _setup_client_with_instrument_results(self, monkeypatch, tmp_path, symbol="AAPL",
                                               last_ts="2026-10-20T14:00:15Z",
                                               bid_ts="2026-10-20T14:00:16Z",
                                               ask_ts="2026-10-20T14:00:16Z",
                                               last_price=123.45, bid_price=123.40,
                                               ask_price=123.50, volume=1000):
        """Setup mock client with instrument_result fields including timestamps."""
        monkeypatch.setattr("public_api_test.quote_diagnostics.RAW_OBSERVATIONS_DIR",
                            tmp_path / "raw")
        monkeypatch.setattr("public_api_test.quote_diagnostics.SUMMARY_STATS_DIR",
                            tmp_path / "summaries")
        tmp_path.joinpath("raw").mkdir()
        tmp_path.joinpath("summaries").mkdir()

        clock = {"t": 0.0}
        wall = {"dt": datetime.fromisoformat("2026-10-20T14:00:00+00:00")}

        def my_clock():
            return clock["t"]

        def my_sleep(seconds):
            clock["t"] += seconds

        def my_wall():
            return wall["dt"]

        client = PublicComClient(api_key="secret", account_id="account", requests_per_minute=40)
        client.token = "token"
        client.account_id = "account"
        client.next_request = 0.0
        client.spacing = 60 / 40

        client.prepare = Mock(return_value=[])
        client._sanitize_batch = Mock(side_effect=lambda x: x)

        responses = [
            {
                "category": "success",
                "http_status": 200,
                "http_latency_ms": 50.0,
                "results": [],
                "instrument_results": [
                    {
                        "symbol": symbol,
                        "category": "success",
                        "price": str(last_price),
                        "api_error_code": None,
                        "provider_timestamp_raw": last_ts,
                        "bid_timestamp_raw": bid_ts,
                        "ask_timestamp_raw": ask_ts,
                        "bid_price": str(bid_price) if bid_price else None,
                        "ask_price": str(ask_price) if ask_price else None,
                        "volume": str(volume) if volume else None,
                    }
                ],
                "returned_symbol_list": [symbol],
                "missing_symbols": [],
            }
        ]
        client.get_quotes_batch = Mock(side_effect=responses)

        return client, clock, wall, my_clock, my_sleep, my_wall

    def test_valid_timestamps_are_extracted(self, monkeypatch, tmp_path):
        """Test that valid Last, Bid, and Ask timestamps are extracted from instrument_result."""
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts="2026-10-20T13:59:50Z",
            bid_ts="2026-10-20T13:59:51Z",
            ask_ts="2026-10-20T13:59:51Z",
            last_price=123.45, bid_price=123.40, ask_price=123.50, volume=1000,
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        assert len(raw_files) == 1

        record = json.loads(raw_files[0].read_text().strip())
        assert record["last_timestamp_raw"] == "2026-10-20T13:59:50Z"
        assert record["bid_timestamp_raw"] == "2026-10-20T13:59:51Z"
        assert record["ask_timestamp_raw"] == "2026-10-20T13:59:51Z"
        assert record["last_timestamp_status"] == "valid"
        assert record["bid_timestamp_status"] == "valid"
        assert record["ask_timestamp_status"] == "valid"
        assert record["last_price"] == 123.45
        assert record["bid_price"] == 123.40
        assert record["ask_price"] == 123.50
        assert record["volume"] == 1000.0
        # Age should be ~10 seconds (wall clock 14:00:00 - timestamp 13:59:50)
        assert record["last_age_seconds"] is not None
        assert record["last_age_seconds"] > 5
        assert record["last_age_seconds"] < 15

    def test_missing_timestamps_classified_as_missing(self, monkeypatch, tmp_path):
        """Test that missing timestamp fields are classified as missing."""
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts=None,
            bid_ts=None,
            ask_ts=None,
            last_price=123.45,
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        assert len(raw_files) == 1

        record = json.loads(raw_files[0].read_text().strip())
        assert record["last_timestamp_raw"] is None
        assert record["last_timestamp_status"] == "missing_timestamp"
        assert record["bid_timestamp_status"] == "missing_timestamp"
        assert record["ask_timestamp_status"] == "missing_timestamp"
        assert record["last_age_seconds"] is None
        assert record["bid_age_seconds"] is None
        assert record["ask_age_seconds"] is None

    def test_invalid_timestamps_classified(self, monkeypatch, tmp_path):
        """Test that invalid timestamp values are classified correctly."""
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts="not-a-timestamp",
            bid_ts="2026-10-20T13:59:51Z",
            ask_ts=None,
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        record = json.loads(raw_files[0].read_text().strip())
        assert record["last_timestamp_status"] == "invalid_timestamp"
        assert record["bid_timestamp_status"] == "valid"
        assert record["ask_timestamp_status"] == "missing_timestamp"

    def test_future_timestamps_retain_negative_age(self, monkeypatch, tmp_path):
        """Test that future timestamps retain negative ages and correct classification."""
        future_ts = "2026-10-20T15:00:00Z"
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts=future_ts,
            bid_ts="2026-10-20T14:00:16Z",
            ask_ts="2026-10-20T14:00:16Z",
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        record = json.loads(raw_files[0].read_text().strip())
        assert record["last_timestamp_status"] == "future_timestamp"
        assert record["last_age_seconds"] is not None
        assert record["last_age_seconds"] < 0

    def test_price_change_without_timestamp_does_not_count(self, monkeypatch, tmp_path):
        """Test that price change with missing timestamp does not count as timestamp anomaly."""
        responses = [
            {
                "category": "success", "http_status": 200, "http_latency_ms": 50.0,
                "results": [],
                "instrument_results": [{
                    "symbol": "AAPL", "category": "success", "price": "100.00",
                    "api_error_code": None, "provider_timestamp_raw": None,
                    "bid_timestamp_raw": None, "ask_timestamp_raw": None,
                    "bid_price": None, "ask_price": None, "volume": None,
                }],
                "returned_symbol_list": ["AAPL"], "missing_symbols": [],
            },
            {
                "category": "success", "http_status": 200, "http_latency_ms": 50.0,
                "results": [],
                "instrument_results": [{
                    "symbol": "AAPL", "category": "success", "price": "200.00",
                    "api_error_code": None, "provider_timestamp_raw": None,
                    "bid_timestamp_raw": None, "ask_timestamp_raw": None,
                    "bid_price": None, "ask_price": None, "volume": None,
                }],
                "returned_symbol_list": ["AAPL"], "missing_symbols": [],
            },
        ]

        monkeypatch.setattr("public_api_test.quote_diagnostics.RAW_OBSERVATIONS_DIR",
                            tmp_path / "raw")
        monkeypatch.setattr("public_api_test.quote_diagnostics.SUMMARY_STATS_DIR",
                            tmp_path / "summaries")
        tmp_path.joinpath("raw").mkdir()
        tmp_path.joinpath("summaries").mkdir()

        clock = {"t": 0.0}
        wall = {"dt": datetime.fromisoformat("2026-10-20T14:00:00+00:00")}

        def my_clock():
            return clock["t"]

        def my_sleep(seconds):
            clock["t"] += seconds

        def my_wall():
            return wall["dt"]

        client = PublicComClient(api_key="secret", account_id="account", requests_per_minute=40)
        client.token = "token"
        client.account_id = "account"
        client.next_request = 0.0
        client.spacing = 60 / 40

        client.prepare = Mock(return_value=[])
        client._sanitize_batch = Mock(side_effect=lambda x: x)
        client.get_quotes_batch = Mock(side_effect=responses)

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=20,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        summary_files = list((tmp_path / "summaries").glob("*.json"))
        summary = json.loads(summary_files[0].read_text())

        assert summary["per_symbol"]["AAPL"]["last_price_changed"] == 1
        assert summary["per_symbol"]["AAPL"]["last_timestamp_advanced"] == 0
        assert summary["per_symbol"]["AAPL"]["missing_last"] == 2

    def test_jsonl_output_contains_timestamps(self, monkeypatch, tmp_path):
        """Test that JSONL output contains extracted timestamps and is valid JSONL."""
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts="2026-10-20T13:59:50Z",
            bid_ts="2026-10-20T13:59:51Z",
            ask_ts="2026-10-20T13:59:51Z",
            last_price=123.45, bid_price=123.40, ask_price=123.50, volume=1000,
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        content = raw_files[0].read_text().strip()
        lines = content.split("\n")
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["last_timestamp_raw"] == "2026-10-20T13:59:50Z"
        assert record["bid_timestamp_raw"] == "2026-10-20T13:59:51Z"
        assert record["ask_timestamp_raw"] == "2026-10-20T13:59:51Z"

    def test_summary_counts_agree_with_raw(self, monkeypatch, tmp_path):
        """Test that summary counts agree with the serialized raw observations."""
        client, clock, wall, my_clock, my_sleep, my_wall = self._setup_client_with_instrument_results(
            monkeypatch, tmp_path,
            last_ts="2026-10-20T13:59:50Z",
            bid_ts="2026-10-20T13:59:51Z",
            ask_ts="2026-10-20T13:59:51Z",
            last_price=123.45, bid_price=123.40, ask_price=123.50, volume=1000,
        )

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=10,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        summary_files = list((tmp_path / "summaries").glob("*.json"))

        raw_record = json.loads(raw_files[0].read_text().strip())
        summary = json.loads(summary_files[0].read_text())

        assert summary["total_observations"] == 1
        assert summary["valid_last_total"] == 1
        assert summary["missing_last_total"] == 0
        assert summary["stale_last_total"] == 0
        assert summary["future_last_total"] == 0

        assert summary["per_symbol"]["AAPL"]["observations"] == 1
        assert summary["per_symbol"]["AAPL"]["valid_last"] == 1
        assert summary["per_symbol"]["AAPL"]["missing_last"] == 0
