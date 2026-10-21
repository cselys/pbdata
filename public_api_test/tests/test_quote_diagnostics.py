"""Unit tests for quote_diagnostics module — offline, mocked HTTP only."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import json

import pytest

from public_api_test.client import PublicComClient
from public_api_test.quote_diagnostics import (
    validate_symbols,
    run_quote_diagnostics,
    Observation,
    _stats,
    _percent,
    _parse_ts,
)


class TestSymbolValidation:
    def test_valid_symbols(self):
        assert validate_symbols(["AAPL", "NVDA", "MSFT"]) == ["AAPL", "NVDA", "MSFT"]

    def test_empty_symbols_rejected(self):
        with pytest.raises(ValueError):
            validate_symbols([])

    def test_duplicates_rejected(self):
        with pytest.raises(ValueError):
            validate_symbols(["AAPL", "AAPL"])

    def test_too_many_symbols_rejected(self):
        with pytest.raises(ValueError):
            validate_symbols([f"S{i}" for i in range(101)])

    def test_case_normalized(self):
        assert validate_symbols(["aapl", "nvda"]) == ["AAPL", "NVDA"]

    def test_whitespace_trimmed(self):
        assert validate_symbols([" AAPL ", " NVDA "]) == ["AAPL", "NVDA"]

    def test_empty_strings_ignored(self):
        assert validate_symbols(["AAPL", "", "   ", "NVDA"]) == ["AAPL", "NVDA"]

    def test_single_symbol_valid(self):
        assert validate_symbols(["AAPL"]) == ["AAPL"]


class TestTimestampParsing:
    def test_valid_iso_timestamp(self):
        ts = _parse_ts("2026-10-20T14:45:21Z")
        assert ts is not None

    def test_invalid_timestamp_returns_none(self):
        assert _parse_ts("not-a-timestamp") is None

    def test_none_returns_none(self):
        assert _parse_ts(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_ts("") is None

    def test_numeric_passthrough(self):
        assert _parse_ts(1000000.0) == 1000000.0


class TestObservation:
    def test_to_dict_includes_all_fields(self):
        obs = Observation(
            symbol="AAPL", sweep=1, scheduled_offset=0.0,
            actual_start_offset=0.5, request_start_utc="2026-10-20T14:00:00+00:00",
            request_finished_utc="2026-10-20T14:00:01+00:00",
            http_status=200, http_latency_ms=500.0,
            batch_index=0, symbols_in_batch=5, category="success",
            last_price=123.45, last_timestamp_raw="2026-10-20T14:45:21Z",
            last_age_seconds=2.5, last_timestamp_status="valid",
            bid_price=123.40, bid_timestamp_raw="2026-10-20T14:45:22Z",
            bid_age_seconds=1.5, bid_timestamp_status="valid",
            ask_price=123.50, ask_timestamp_raw="2026-10-20T14:45:22Z",
            ask_age_seconds=1.5, ask_timestamp_status="valid",
            volume=1000, volume_changed=True,
            price_changed=True, last_timestamp_changed=True,
            batch_timestamp_advanced=True,
            market_session="regular", provider_session="regular",
            request_category="success", api_error_code=None,
            missing_symbols=[],
        )
        d = obs.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["sweep"] == 1
        assert d["http_status"] == 200
        assert d["last_price"] == 123.45
        assert d["last_timestamp_status"] == "valid"
        assert d["bid_timestamp_status"] == "valid"
        assert d["ask_timestamp_status"] == "valid"
        assert d["market_session"] == "regular"
        assert d["missing_symbols"] == []

    def test_to_jsonl_is_valid_json(self):
        obs = Observation(
            symbol="MSFT", sweep=1, scheduled_offset=0.0,
            actual_start_offset=0.0, request_start_utc="2026-10-20T14:00:00+00:00",
            request_finished_utc="2026-10-20T14:00:01+00:00",
            http_status=200, http_latency_ms=100.0,
            batch_index=0, symbols_in_batch=1, category="success",
            last_price=None, last_timestamp_raw=None,
            last_age_seconds=None, last_timestamp_status="missing_timestamp",
            bid_price=123.4, bid_timestamp_raw="2026-10-20T14:45:21Z",
            bid_age_seconds=5.0, bid_timestamp_status="valid",
            ask_price=123.5, ask_timestamp_raw="2026-10-20T14:45:21Z",
            ask_age_seconds=5.0, ask_timestamp_status="valid",
            volume=None, volume_changed=None,
            price_changed=False, last_timestamp_changed=False,
            batch_timestamp_advanced=True,
            market_session="regular", provider_session="regular",
            request_category="success", api_error_code=None,
            missing_symbols=[],
        )
        parsed = json.loads(obs.to_jsonl())
        assert parsed["symbol"] == "MSFT"
        assert parsed["last_price"] is None
        assert parsed["last_timestamp_status"] == "missing_timestamp"


class TestStatsHelpers:
    def test_stats_empty(self):
        assert _stats([]) is None

    def test_stats_single_value(self):
        result = _stats([5.0])
        assert result == {
            "count": 1, "min": 5.0, "max": 5.0, "mean": 5.0,
            "median": 5.0, "p95": 5.0,
        }

    def test_stats_multiple_values(self):
        result = _stats([1.0, 2.0, 3.0, 4.0, 5.0])
        assert result["count"] == 5
        assert result["min"] == 1.0
        assert result["max"] == 5.0
        assert result["mean"] == 3.0
        assert result["median"] == 3.0

    def test_stats_with_none(self):
        result = _stats([1.0, None, 3.0])
        assert result["count"] == 2

    def test_percent(self):
        assert _percent(5, 10) == 50.0
        assert _percent(0, 10) == 0.0
        assert _percent(10, 10) == 100.0
        assert _percent(1, 0) is None


class TestRunDiagnosticsOffline:
    def test_validates_symbols_before_running(self):
        with pytest.raises(ValueError):
            run_quote_diagnostics(symbols=[], interval_seconds=1, duration_seconds=1)

    def test_validates_interval_positive(self):
        with pytest.raises(ValueError):
            run_quote_diagnostics(symbols=["AAPL"], interval_seconds=0, duration_seconds=1)

    def test_validates_duration_positive(self):
        with pytest.raises(ValueError):
            run_quote_diagnostics(symbols=["AAPL"], interval_seconds=1, duration_seconds=0)

    def test_offline_run_completes_with_mocked_client(self, monkeypatch, tmp_path):
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

        responses = []
        for sweep in range(3):
            for symbol in ["AAPL", "NVDA"]:
                responses.append(
                    {
                        "category": "success",
                        "http_status": 200,
                        "http_latency_ms": 50.0,
                        "results": [],
                        "instrument_results": [
                            {
                                "symbol": symbol,
                                "category": "success",
                                "price": "123.45",
                                "api_error_code": None,
                                "raw_response": {
                                    "instrument": {"symbol": symbol, "type": "EQUITY"},
                                    "outcome": "SUCCESS",
                                    "last": "123.45",
                                    "lastTimestamp": "2026-10-20T14:00:15Z",
                                    "bid": "123.40",
                                    "bidSize": 100,
                                    "bidTimestamp": "2026-10-20T14:00:16Z",
                                    "ask": "123.50",
                                    "askSize": 100,
                                    "askTimestamp": "2026-10-20T14:00:16Z",
                                    "volume": 1000,
                                },
                            }
                        ],
                        "returned_symbol_list": [symbol],
                        "missing_symbols": [],
                    }
                )
            wall["dt"] += timedelta(seconds=10)
            clock["t"] += 10

        client.get_quotes_batch = Mock(side_effect=responses)

        result = run_quote_diagnostics(
            symbols=["AAPL", "NVDA"],
            interval_seconds=10,
            duration_seconds=30,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True
        # 2 symbols fit in one batch (max 20), 3 sweeps = 3 calls
        assert client.get_quotes_batch.call_count == 3

        raw_files = list((tmp_path / "raw").glob("*.jsonl"))
        summary_files = list((tmp_path / "summaries").glob("*.json"))
        assert len(raw_files) == 1
        assert len(summary_files) == 1

        summary = json.loads(summary_files[0].read_text())
        assert summary["run_status"] == "success"
        assert summary["total_sweeps"] == 3
        assert summary["total_observations"] == 6
        assert len(summary["per_symbol"]) == 2
        assert summary["per_symbol"]["AAPL"]["observations"] == 3
        assert summary["per_symbol"]["NVDA"]["observations"] == 3

    def test_stale_last_with_fresh_bid_ask_detected(self, monkeypatch, tmp_path):
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

        responses = []
        for sweep in range(3):
            wall_dt = wall["dt"]
            old_last_ts = (wall_dt - timedelta(seconds=120)).isoformat()
            responses.append(
                {
                    "category": "success",
                    "http_status": 200,
                    "http_latency_ms": 50.0,
                    "results": [],
                    "instrument_results": [
                        {
                            "symbol": "AAPL",
                            "category": "success",
                            "price": "123.45",
                            "api_error_code": None,
                            "raw_response": {
                                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                                "outcome": "SUCCESS",
                                "last": "123.45",
                                "lastTimestamp": old_last_ts,
                                "bid": "123.40",
                                "bidSize": 100,
                                "bidTimestamp": wall_dt.isoformat(),
                                "ask": "123.50",
                                "askSize": 100,
                                "askTimestamp": wall_dt.isoformat(),
                                "volume": 1000,
                            },
                        }
                    ],
                    "returned_symbol_list": ["AAPL"],
                    "missing_symbols": [],
                }
            )
            wall["dt"] += timedelta(seconds=10)
            clock["t"] += 10

        client.get_quotes_batch = Mock(side_effect=responses)

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=30,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        summary = json.loads(list((tmp_path / "summaries").glob("*.json"))[0].read_text())
        assert summary["stale_last_total"] == 3
        assert summary["stale_last_with_fresh_bid_ask"] == 3
        assert summary["per_symbol"]["AAPL"]["stale_last"] == 3
        assert summary["per_symbol"]["AAPL"]["stale_bid"] == 0
        assert summary["per_symbol"]["AAPL"]["stale_ask"] == 0

    def test_timeout_handling(self, monkeypatch, tmp_path):
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

        old_ts = (wall["dt"] - timedelta(seconds=5)).isoformat()
        responses = [
            {
                "category": "network_error",
                "http_status": None,
                "api_error_code": "Timeout",
                "quote_request_count": None,
                "http_latency_ms": None,
                "results": [],
                "instrument_results": [],
                "returned_symbol_list": [],
                "missing_symbols": ["AAPL"],
            },
            {
                "category": "success",
                "http_status": 200,
                "http_latency_ms": 50.0,
                "results": [],
                "instrument_results": [
                    {
                        "symbol": "AAPL",
                        "category": "success",
                        "price": "123.45",
                        "api_error_code": None,
                        "raw_response": {
                            "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                            "outcome": "SUCCESS",
                            "last": "123.45",
                            "lastTimestamp": old_ts,
                            "bid": "123.40",
                            "bidSize": 100,
                            "bidTimestamp": wall["dt"].isoformat(),
                            "ask": "123.50",
                            "askSize": 100,
                            "askTimestamp": wall["dt"].isoformat(),
                            "volume": 1000,
                        },
                    }
                ],
                "returned_symbol_list": ["AAPL"],
                "missing_symbols": [],
            },
        ]

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

        summary = json.loads(list((tmp_path / "summaries").glob("*.json"))[0].read_text())
        assert summary["http_status_distribution"]["None"] == 1
        assert summary["http_status_distribution"]["200"] == 1
        assert summary["total_observations"] == 2
        assert summary["per_symbol"]["AAPL"]["missing_last"] == 1

    def test_price_changed_without_timestamp_advance_detected(self, monkeypatch, tmp_path):
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

        same_ts = "2026-10-20T14:00:10Z"
        responses = []
        prices = ["100.00", "100.50", "101.00"]
        for sweep_idx in range(3):
            responses.append(
                {
                    "category": "success",
                    "http_status": 200,
                    "http_latency_ms": 50.0,
                    "results": [],
                    "instrument_results": [
                        {
                            "symbol": "AAPL",
                            "category": "success",
                            "price": prices[sweep_idx],
                            "api_error_code": None,
                            "raw_response": {
                                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                                "outcome": "SUCCESS",
                                "last": prices[sweep_idx],
                                "lastTimestamp": same_ts,
                                "bid": "99.90",
                                "bidSize": 100,
                                "bidTimestamp": wall["dt"].isoformat(),
                                "ask": "100.10",
                                "askSize": 100,
                                "askTimestamp": wall["dt"].isoformat(),
                                "volume": 1000,
                            },
                        }
                    ],
                    "returned_symbol_list": ["AAPL"],
                    "missing_symbols": [],
                }
            )
            wall["dt"] += timedelta(seconds=10)
            clock["t"] += 10

        client.get_quotes_batch = Mock(side_effect=responses)

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=30,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        summary = json.loads(list((tmp_path / "summaries").glob("*.json"))[0].read_text())
        assert summary["price_changed_when_last_ts_unchanged"] == 2

    def test_consecutive_stale_sequence_tracked(self, monkeypatch, tmp_path):
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

        old_ts = (wall["dt"] - timedelta(seconds=120)).isoformat()
        fresh_ts = wall["dt"].isoformat()
        responses = []
        for sweep_idx in range(5):
            ts = old_ts if sweep_idx < 3 else fresh_ts
            responses.append(
                {
                    "category": "success",
                    "http_status": 200,
                    "http_latency_ms": 50.0,
                    "results": [],
                    "instrument_results": [
                        {
                            "symbol": "AAPL",
                            "category": "success",
                            "price": "123.45",
                            "api_error_code": None,
                            "raw_response": {
                                "instrument": {"symbol": "AAPL", "type": "EQUITY"},
                                "outcome": "SUCCESS",
                                "last": "123.45",
                                "lastTimestamp": ts,
                                "bid": "123.40",
                                "bidSize": 100,
                                "bidTimestamp": wall["dt"].isoformat(),
                                "ask": "123.50",
                                "askSize": 100,
                                "askTimestamp": wall["dt"].isoformat(),
                                "volume": 1000,
                            },
                        }
                    ],
                    "returned_symbol_list": ["AAPL"],
                    "missing_symbols": [],
                }
            )
            wall["dt"] += timedelta(seconds=10)
            clock["t"] += 10

        client.get_quotes_batch = Mock(side_effect=responses)

        result = run_quote_diagnostics(
            symbols=["AAPL"],
            interval_seconds=10,
            duration_seconds=50,
            requests_per_minute=40,
            max_age=60,
            clock=my_clock,
            sleep=my_sleep,
            wall_clock=my_wall,
            client=client,
        )

        assert result is True

        summary = json.loads(list((tmp_path / "summaries").glob("*.json"))[0].read_text())
        assert summary["per_symbol"]["AAPL"]["max_consecutive_stale_last"] == 3