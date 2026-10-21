"""Offline tests for the bounded read-only latency probe."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.volume_latency_probe_20261019 import probe  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    def __init__(self, statuses=(200,), body=None, setup=None):
        self.token = "fake-token"
        self.statuses = list(statuses)
        self.body = body if body is not None else {
            "regularMarket": {"bars": [
                {"timestamp": "2026-09-21T13:29:00Z", "volume": 10},
                {"timestamp": "2026-09-21T13:30:00Z", "volume": 11},
            ]}
        }
        self.setup = setup or []
        self.calls = 0

    def prepare(self):
        return self.setup

    def _request(self, method, endpoint, params=None):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        if status is None:
            return None, 0.0, {"error": "Timeout"}, {}
        return FakeResponse(status), 1.5, self.body, {}


def _run(tmp_path, client, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("now", lambda: NOW)
    counter = iter(range(1000))
    kwargs.setdefault("perf_counter", lambda: float(next(counter)) * 0.001)
    return probe.run_probe(client, tmp_path, **kwargs)


def test_plan_budget_arithmetic():
    plan = probe.plan()
    assert plan["symbols"] == ["SPY", "QQQ", "NVDA"]
    assert plan["rounds"] == 2
    assert plan["setup_request_cap"] == 2
    assert plan["bars_requests"] == 6
    assert plan["hard_request_cap"] == 8
    assert plan["retries"] == 0


def test_probe_records_timings_and_no_secrets(tmp_path):
    setup = [{
        "stage": "authentication",
        "http_status": 200,
        "category": "success",
        "request_duration_ms": 12.0,
        "endpoint_url": "https://api.public.com/?token=supersecret",
        "api_error_message": "Authorization: Bearer supersecret",
    }]
    body = {
        "regularMarket": {"bars": [
            {"timestamp": "2026-09-21T13:30:00Z", "volume": 1,
             "token": "supersecret"},
        ]},
        "token": "supersecret",
    }
    client = FakeClient(statuses=[200] * 6, body=body, setup=setup)
    result = _run(tmp_path, client)
    assert result["outcome"] == "COMPLETED"
    assert result["requests_total"] == 1 + 6
    assert client.calls == 6

    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    lines = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    for secret in ("supersecret", "Bearer", "Authorization", "https://"):
        assert secret not in text
        assert secret not in lines
    manifest = json.loads(text)
    assert manifest["setup_requests"] == 1
    assert manifest["bars_requests"] == 6
    assert manifest["status_counts"]["200"] == 7
    assert manifest["latency_ms"]["count"] == 6
    first = json.loads(lines.splitlines()[0])
    assert first["endpoint"] == "one_minute_bars"
    assert first["started_at_utc"] and first["ended_at_utc"]
    assert first["bar_count"] == 1
    assert set((tmp_path).iterdir()) == {
        tmp_path / "manifest.json", tmp_path / "requests.jsonl"}


def test_probe_stops_on_429_without_retry(tmp_path):
    client = FakeClient(statuses=[429])
    result = _run(tmp_path, client)
    assert result["outcome"] == "RATE_LIMITED"
    assert result["requests_total"] == 1
    assert client.calls == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status_counts"]["429"] == 1
    assert manifest["shutdown_reason"] == "RATE_LIMITED"


def test_probe_stops_on_two_consecutive_errors(tmp_path):
    client = FakeClient(statuses=[500, 500])
    result = _run(tmp_path, client)
    assert result["outcome"] == "REPEATED_ERRORS"
    assert result["requests_total"] == 2
    assert client.calls == 2
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status_counts"]["500"] == 2


def test_probe_enforces_its_own_request_cap(tmp_path):
    client = FakeClient(statuses=[200] * 10)
    result = _run(tmp_path, client, max_requests=2)
    assert result["outcome"] == "REQUEST_CAP"
    assert result["requests_total"] == 2
    assert client.calls == 2


def test_probe_setup_429_stops_before_bars(tmp_path):
    setup = [{"stage": "authentication", "http_status": 429,
              "category": "rate_limited", "request_duration_ms": 1.0}]
    client = FakeClient(statuses=[200] * 6, setup=setup)
    result = _run(tmp_path, client)
    assert result["outcome"] == "RATE_LIMITED"
    assert result["requests_total"] == 1
    assert client.calls == 0


def test_probe_auth_failure_sends_no_bars(tmp_path):
    client = FakeClient(statuses=[200] * 6)
    client.token = None
    result = _run(tmp_path, client)
    assert result["outcome"] == "AUTH_UNAVAILABLE"
    assert result["requests_total"] == 0
    assert client.calls == 0
    assert not (tmp_path / "requests.jsonl").read_text(encoding="utf-8")


def test_plan_mode_is_offline_and_prints_json(capsys):
    assert probe.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hard_request_cap"] == 8
    assert payload["no_cache_writes"] is True
