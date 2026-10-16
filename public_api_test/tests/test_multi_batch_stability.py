"""Multi-batch full-universe sweeps with mocked HTTP and deterministic clocks."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
import requests
from public_api_test.client import PublicComClient
from public_api_test.multi_batch_stability import (
    monitor_multibatch, summarize_multibatch, validate_symbols, divide_batches,
    run_multi_batch_stability)

SYMBOLS_100 = [f"T{i:03d}" for i in range(100)]


class Clock:
    def __init__(self, base="2026-10-09T14:00:00+00:00"):
        self.seconds = 0.0
        self.base = datetime.fromisoformat(base)
    def tick(self):
        return self.seconds
    def sleep(self, value):
        self.seconds += value
    def utc(self):
        return self.base + timedelta(seconds=self.seconds)


def quote(symbol, volume=100, stamp="2026-10-09T14:00:00Z"):
    return dict(instrument={"symbol": symbol, "type": "EQUITY"}, outcome="SUCCESS", last="123",
        bid="122", ask="124", lastTimestamp=stamp, bidTimestamp=stamp, askTimestamp=stamp, volume=volume)


def reply(quotes=None, status=200):
    response = Mock(status_code=status, headers={})
    response.json.return_value = {"quotes": quotes or []} if status == 200 else {"code": "ERROR"}
    return response


def request_symbols(kwargs):
    return [item["symbol"] for item in (kwargs.get("json") or {}).get("instruments", [])]


def successful_handler(method, url, **kwargs):
    return reply([quote(s) for s in request_symbols(kwargs)])


@pytest.fixture
def rig(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("public_api_test.client.time.monotonic", clock.tick)
    monkeypatch.setattr("public_api_test.client.time.sleep", clock.sleep)
    class Date:
        @staticmethod
        def now(tz):
            return clock.utc()
    monkeypatch.setattr("public_api_test.client.datetime", Date)
    client = PublicComClient(api_key="secret-value", account_id="account-value", requests_per_minute=40)
    client.token = "token-value"
    client.session.request = Mock(side_effect=successful_handler)
    client.prepare = Mock(side_effect=AssertionError("Monitoring must not reauthenticate"))
    return client, clock


def poll(rig, batches, duration=21, interval=10):
    client, clock = rig
    return monitor_multibatch(client, batches, interval, duration, lambda record: None,
        clock=clock.tick, sleep=clock.sleep, wall_clock=clock.utc)


def test_hundred_symbols_five_posts_per_sweep_fifteen_over_three(rig):
    client, clock = rig
    batches = divide_batches(SYMBOLS_100, 20)
    assert [len(b) for b in batches] == [20] * 5
    sweeps, interrupted, elapsed, skipped = poll(rig, batches, duration=21)
    assert len(sweeps) == 3 and not interrupted and skipped == 0
    # Three complete sweeps x five batches = fifteen authenticated Quotes POST requests.
    assert client.session.request.call_count == 15
    for call in client.session.request.call_args_list:
        assert call.args[0] == "POST" and call.args[1].endswith("/quotes")
        assert len(call.kwargs["json"]["instruments"]) == 20
    for sweep in sweeps:
        assert sweep["quote_request_count"] == 5
        assert [b["batch_index"] for b in sweep["batches"]] == [1, 2, 3, 4, 5]
        requested = [s for b in sweep["batches"] for s in b["symbols_requested"]]
        assert sorted(requested) == sorted(SYMBOLS_100)
        assert all(b["http_status"] == 200 and b["category"] == "success" for b in sweep["batches"])
    # The 10-second interval applies to complete sweeps, not to individual batches.
    assert [s["actual_polling_interval_seconds"] for s in sweeps] == [None, 10, 10]
    client.prepare.assert_not_called()


def test_global_rate_limiter_respected(rig):
    client, clock = rig
    spacing = 60 / 40  # 40 requests/minute global cap -> 1.5s between request starts
    sweeps, _, _, skipped = poll(rig, divide_batches(SYMBOLS_100, 20), duration=21)
    assert skipped == 0
    for sweep in sweeps:
        offsets = [b["batch_start_offset_seconds"] for b in sweep["batches"]]
        assert offsets == pytest.approx([0, spacing, 2 * spacing, 3 * spacing, 4 * spacing])
    assert [s["actual_polling_interval_seconds"] for s in sweeps
        if s["actual_polling_interval_seconds"] is not None] == [10, 10]


def test_run_persists_jsonl_and_summary_authenticates_once(monkeypatch, tmp_path, rig):
    client, clock = rig
    client.prepare = Mock(return_value=[])
    monkeypatch.setattr("public_api_test.multi_batch_stability.PublicComClient", lambda **kw: client)
    monkeypatch.setattr("public_api_test.multi_batch_stability.RAW_OBSERVATIONS_DIR", tmp_path)
    monkeypatch.setattr("public_api_test.multi_batch_stability.SUMMARY_STATS_DIR", tmp_path)
    real = monitor_multibatch
    def simulated(*a, **kw):
        return real(*a, **kw, clock=clock.tick, sleep=clock.sleep, wall_clock=clock.utc)
    monkeypatch.setattr("public_api_test.multi_batch_stability.monitor_multibatch", simulated)
    assert run_multi_batch_stability(SYMBOLS_100, interval_seconds=10, duration_seconds=21,
        requests_per_minute=40, max_symbols_per_batch=20)
    client.prepare.assert_called_once()
    raw_text = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    raw = [json.loads(line) for line in raw_text.splitlines()]
    summary_text = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    assert len(raw) == summary["total_sweeps"] == 3
    assert summary["run_status"] == "success" and summary["successful_sweeps"] == 3
    assert summary["quotes_http_requests"] == 15
    assert [len(s["batches"]) for s in raw] == [5, 5, 5]
    assert raw[0]["quote_request_count"] == 5
    assert all(b["http_status"] == 200 for b in raw[0]["batches"])
    rows = raw[0]["observations"]
    assert len(rows) == 100
    first = rows[0]
    assert first["symbol"] == SYMBOLS_100[0]
    assert first["price"] == 123 and first["bid"] == "122" and first["ask"] == "124"
    assert first["volume"] == 100
    assert first["last_timestamp_raw"] == "2026-10-09T14:00:00Z"
    assert first["volume_delta_status"] == "first_sample"
    assert raw[1]["observations"][0]["volume_delta"] == 0
    assert raw[1]["observations"][0]["volume_delta_status"] == "valid"
    assert summary["per_symbol"][SYMBOLS_100[0]]["volume_interval_status_counts"] == \
        {"first_sample": 1, "valid": 2}
    for secret in ("secret-value", "token-value", "account-value"):
        assert secret not in raw_text and secret not in summary_text


def test_partial_batch_failures_do_not_terminate_sweeps(rig):
    client, clock = rig

    def selective(method, url, **kwargs):
        requested = request_symbols(kwargs)
        if requested and requested[0] == SYMBOLS_100[40]:  # third batch fails on every sweep
            return reply(status=500)
        return reply([quote(s) for s in requested])

    client.session.request.side_effect = selective
    batches = divide_batches(SYMBOLS_100, 20)
    sweeps, interrupted, _, _ = poll(rig, batches, duration=21)
    assert not interrupted and len(sweeps) == 3
    assert client.session.request.call_count == 15  # polling never silently skipped
    for sweep in sweeps:
        assert [b["category"] for b in sweep["batches"]] == \
            ["success", "success", "api_error", "success", "success"]
        third = sweep["batches"][2]
        assert third["http_status"] == 500 and third["valid_price_count"] == 0
        assert all(b["category"] == "success" for b in sweep["batches"][:2] + sweep["batches"][3:])
    summary = summarize_multibatch(sweeps, SYMBOLS_100)
    assert summary["request_failure_batches"] == 3 and summary["successful_sweeps"] == 0
    assert summary["http_status_distribution"]["500"] == 3
    for symbol in batches[2]:
        row = next(o for o in sweeps[0]["observations"] if o["symbol"] == symbol)
        assert row["category"] == "request_failed"
        assert row["volume_delta_status"] == "failed_sample"


def test_missing_symbols_are_reported(rig):
    client, clock = rig

    def dropping(method, url, **kwargs):
        present = [s for s in request_symbols(kwargs) if s != SYMBOLS_100[7]]
        return reply([quote(s) for s in present])

    client.session.request.side_effect = dropping
    sweeps, _, _, _ = poll(rig, divide_batches(SYMBOLS_100, 20), duration=11)
    assert len(sweeps) == 2
    batch = next(b for b in sweeps[0]["batches"] if SYMBOLS_100[7] in b["symbols_requested"])
    assert batch["missing_symbols"] == [SYMBOLS_100[7]]
    assert batch["category"] == "partial_result"
    assert batch["completeness_ratio"] == pytest.approx(19 / 20)
    assert batch["valid_price_count"] == 19
    row = next(o for o in sweeps[0]["observations"] if o["symbol"] == SYMBOLS_100[7])
    assert row["category"] == "missing_symbol"
    summary = summarize_multibatch(sweeps, SYMBOLS_100)
    assert summary["missing_symbol_observations"] == 2
    assert summary["instrument_failures"] == 2
    sweep_report = next(r for r in summary["sweep_results"] if r["sweep"] == 1)
    assert SYMBOLS_100[7] in sweep_report["missing_symbols"]
    assert len(sweep_report["returned_symbols"]) == 99


def test_http_429_backoff_and_recovery(rig):
    client, clock = rig
    limited = reply(status=429)
    limited.headers = {"Retry-After": "25"}
    calls = {"n": 0}

    def handler(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return limited
        return reply([quote(s) for s in request_symbols(kwargs)])

    client.session.request.side_effect = handler
    sweeps, _, _, skipped = poll(rig, divide_batches(SYMBOLS_100, 20), duration=45)
    first = sweeps[0]["batches"][0]
    assert first["http_status"] == 429 and first["category"] == "rate_limited"
    assert len(sweeps) == 3 and skipped == 2
    assert calls["n"] == 15  # polling never silently skipped
    summary = summarize_multibatch(sweeps, SYMBOLS_100)
    assert summary["http_429_responses"] == 1
    assert sweeps[0]["observations"][0]["category"] == "request_failed"
    assert sweeps[1]["batches"][0]["http_status"] == 200
    assert [s["actual_polling_interval_seconds"] for s in sweeps] == [None, 31, 9]


def test_network_timeout_isolated_and_run_continues(rig):
    client, clock = rig
    calls = {"n": 0}

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("sensitive error")
        return reply([quote(s) for s in request_symbols(kwargs)])

    client.session.request.side_effect = flaky
    sweeps, interrupted, _, _ = poll(rig, divide_batches(SYMBOLS_100, 20), duration=21)
    assert not interrupted and len(sweeps) == 3
    assert calls["n"] == 15
    batch = sweeps[0]["batches"][0]
    assert batch["http_status"] is None and batch["category"] == "network_error"
    assert "Timeout" in str(batch["batch"]["api_error_code"])
    assert len(batch["missing_symbols"]) == 20
    assert sweeps[0]["observations"][0]["category"] == "request_failed"
    summary = summarize_multibatch(sweeps, SYMBOLS_100)
    assert summary["network_timeouts"] == 1 and summary["request_failure_batches"] == 1
    recovered = next(o for o in sweeps[1]["observations"] if o["symbol"] == SYMBOLS_100[0])
    assert recovered["category"] == "success"
    assert recovered["volume_delta_status"] == "previous_failed_sample"
    assert next(o for o in sweeps[2]["observations"] if o["symbol"] == SYMBOLS_100[0])["volume_delta"] == 0
    assert "sensitive error" not in json.dumps(sweeps)


def test_client_exception_isolated(rig, monkeypatch):
    client, clock = rig
    calls = {"n": 0}
    real_batch = client.get_quotes_batch

    def flaky(symbols):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected local failure")
        return real_batch(symbols)

    monkeypatch.setattr(client, "get_quotes_batch", flaky)
    sweeps, interrupted, _, _ = poll(rig, divide_batches(SYMBOLS_100, 20), duration=21)
    assert not interrupted and len(sweeps) == 3 and calls["n"] == 15
    batch = sweeps[0]["batches"][0]
    assert batch["category"] == "client_error" and batch["batch"]["api_error_code"] == "RuntimeError"
    assert batch["missing_symbols"] == SYMBOLS_100[:20]
    assert sweeps[1]["batches"][0]["category"] == "success"


def test_volume_delta_rules_preserved_across_sweeps(rig):
    client, clock = rig
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    volumes = [dict(AAA=100, BBB=200, CCC=50, DDD=80),
               dict(AAA=150, BBB=120, CCC=None, DDD=90),
               dict(AAA=180, BBB=140, CCC=70, DDD=100)]
    calls = {"n": 0}

    def staged(method, url, **kwargs):
        requested = request_symbols(kwargs)
        vols = volumes[min(calls["n"] // 2, len(volumes) - 1)]  # two batches per sweep
        calls["n"] += 1
        return reply([quote(s, volume=vols.get(s, 100)) for s in requested])

    client.session.request.side_effect = staged
    sweeps, _, _, _ = poll(rig, divide_batches(symbols, 2), duration=21)
    assert len(sweeps) == 3

    def row(sweep, symbol):
        return next(o for o in sweeps[sweep]["observations"] if o["symbol"] == symbol)

    assert row(0, "AAA")["volume_delta_status"] == "first_sample"
    assert row(1, "AAA")["volume_delta"] == 50 and row(1, "AAA")["volume_delta_status"] == "valid"
    assert row(1, "BBB")["volume_delta_status"] == "cumulative_volume_reset"
    assert row(1, "BBB")["volume_delta"] is None
    assert row(1, "CCC")["volume_delta_status"] == "missing_or_invalid_volume"
    assert row(1, "DDD")["volume_delta"] == 10
    # After a reset, the next sweep still measures against the prior sample.
    assert row(2, "BBB")["volume_delta"] == 20 and row(2, "BBB")["volume_delta_status"] == "valid"
    summary = summarize_multibatch(sweeps, symbols)
    assert summary["per_symbol"]["AAA"]["volume_delta_statistics"]["mean"] == 40
    assert summary["per_symbol"]["AAA"]["volume_samples"][1]["delta"] == 50
    assert summary["per_symbol"]["BBB"]["volume_interval_status_counts"] == \
        {"first_sample": 1, "cumulative_volume_reset": 1, "valid": 1}
    # The reset sample is excluded, but the recovery delta is still measured.
    assert summary["per_symbol"]["BBB"]["volume_delta_statistics"]["count"] == 1
    assert summary["per_symbol"]["BBB"]["volume_delta_statistics"]["mean"] == 20


def test_validation_and_batch_division():
    assert validate_symbols(["aapl", "  MSFT "]) == ["AAPL", "MSFT"]
    with pytest.raises(ValueError):
        validate_symbols([f"S{i}" for i in range(101)])
    with pytest.raises(ValueError):
        validate_symbols(["AAPL", "AAPL"])
    with pytest.raises(ValueError):
        validate_symbols([])
    with pytest.raises(ValueError):
        divide_batches(SYMBOLS_100, 0)
    with pytest.raises(ValueError):
        divide_batches(SYMBOLS_100, 20.5)
    assert [len(b) for b in divide_batches(SYMBOLS_100, 30)] == [30, 30, 30, 10]


def test_rejects_bad_universe_before_authentication(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("public_api_test.multi_batch_stability.PublicComClient", factory)
    with pytest.raises(ValueError):
        run_multi_batch_stability([f"S{i}" for i in range(101)])
    factory.assert_not_called()


def test_setup_failure_stops_before_any_polling(monkeypatch, tmp_path):
    client = PublicComClient(api_key=None, account_id=None, requests_per_minute=40)
    client.token = None
    client.account_id = None
    client.prepare = Mock(return_value=[dict(stage="authentication", category="configuration_error")])
    client.session.request = Mock()
    monkeypatch.setattr("public_api_test.multi_batch_stability.PublicComClient", lambda **kw: client)
    monkeypatch.setattr("public_api_test.multi_batch_stability.RAW_OBSERVATIONS_DIR", tmp_path)
    monkeypatch.setattr("public_api_test.multi_batch_stability.SUMMARY_STATS_DIR", tmp_path)
    assert not run_multi_batch_stability(SYMBOLS_100, duration_seconds=21)
    client.session.request.assert_not_called()
    summary = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert summary["run_status"] == "setup_failed"


def test_scanner_symbol_file_has_100_unique_real_tickers():
    path = Path(__file__).resolve().parents[1] / "config" / "scanner_symbols_100.txt"
    symbols = [line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(symbols) == 100
    assert len(set(symbols)) == 100
    for symbol in symbols:
        assert re.fullmatch(r"[A-Z]{1,5}", symbol), symbol
        assert not symbol.startswith("SYMBOL")  # no placeholders
    for etf in ("SPY", "QQQ", "IWM", "VOO", "VTI", "EFA", "EEM", "GLD", "SLV", "USO",
                "ARKK", "XLF", "XLE", "XLK", "VXX", "TQQQ", "DIA"):
        assert etf not in symbols
    assert divide_batches(symbols, 20) == [symbols[i:i + 20] for i in range(0, 100, 20)]


def test_cli_routes_multi_batch_phase():
    symbols_file = str(Path(__file__).resolve().parents[1] / "config" / "scanner_symbols_100.txt")
    argv = ["tester", "--phase", "multi_batch_stability", "--symbols-file", symbols_file,
            "--interval", "10", "--duration", "1800", "--requests-per-minute", "40"]
    with patch("sys.argv", argv), \
         patch("public_api_test.multi_batch_stability.run_multi_batch_stability", return_value=True) as run:
        from public_api_test.__main__ import main
        with pytest.raises(SystemExit) as result:
            main()
    assert result.value.code == 0
    call = run.call_args
    assert len(call.kwargs["symbols"]) == 100
    assert call.kwargs["interval_seconds"] == 10
    assert call.kwargs["duration_seconds"] == 1800
    assert call.kwargs["requests_per_minute"] == 40
    assert call.kwargs["max_symbols_per_batch"] == 20
    assert call.kwargs["max_age"] == 60.0 and call.kwargs["clock_skew_tolerance"] == 0.0
