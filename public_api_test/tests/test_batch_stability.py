"""Stability polling with mocked HTTP and deterministic monotonic/UTC clocks."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import pytest
import requests
from public_api_test.client import PublicComClient
from public_api_test.batch_stability import monitor_batches, summarize_batches, validate_symbols, run_batch_stability


class Clock:
    def __init__(self, base="2026-10-09T14:00:00+00:00"):
        self.seconds=0.0
        self.base=datetime.fromisoformat(base)
    def tick(self): return self.seconds
    def sleep(self,value): self.seconds+=value
    def utc(self): return self.base+timedelta(seconds=self.seconds)


def quote(symbol, volume=100, stamp="2026-10-09T14:00:00Z"):
    return dict(instrument={"symbol":symbol,"type":"EQUITY"},outcome="SUCCESS",last="123",
        bid="122",ask="124",lastTimestamp=stamp,bidTimestamp=stamp,askTimestamp=stamp,volume=volume)


def reply(quotes=None,status=200):
    response=Mock(status_code=status,headers={})
    response.json.return_value={"quotes":quotes or []} if status==200 else {"code":"ERROR"}
    return response


@pytest.fixture
def rig(monkeypatch):
    clock=Clock()
    monkeypatch.setattr("public_api_test.client.time.monotonic",clock.tick)
    monkeypatch.setattr("public_api_test.client.time.sleep",clock.sleep)
    class Date:
        @staticmethod
        def now(tz): return clock.utc()
    monkeypatch.setattr("public_api_test.client.datetime",Date)
    client=PublicComClient(api_key="secret-value",account_id="account-value",requests_per_minute=40)
    client.token="token-value"
    client.session.request=Mock()
    client.prepare=Mock(side_effect=AssertionError("Monitoring must not reauthenticate"))
    return client,clock


def poll(rig,symbols,duration=21,interval=10):
    client,clock=rig
    return monitor_batches(client,symbols,interval,duration,lambda row:None,
        clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc)


def test_twenty_symbols_exactly_one_post_per_cycle(rig):
    client,clock=rig
    symbols=[f"S{i}" for i in range(20)]
    client.session.request.return_value=reply([quote(s) for s in symbols])
    rows,interrupted,elapsed,skipped=poll(rig,symbols)
    assert len(rows)==3 and not interrupted and not skipped
    assert client.session.request.call_count==3
    assert [r["actual_start_offset_seconds"] for r in rows]==[0,10,20]
    for call in client.session.request.call_args_list:
        assert call.args[0]=="POST" and call.args[1].endswith("/quotes")
        assert len(call.kwargs["json"]["instruments"])==20
    client.prepare.assert_not_called()
    summary=summarize_batches(rows,symbols)
    assert summary["successful_cycles"]==3
    assert summary["http_success_rate"]==1
    assert summary["actual_polling_intervals_seconds"]["mean"]==10
    assert "secret-value" not in json.dumps(rows)
    assert "account-value" not in json.dumps(rows)
    assert "token-value" not in json.dumps(rows)


def test_partial_timeout_and_recovery_volume_gaps(rig):
    client,_=rig
    client.session.request.side_effect=[reply([quote("AAPL"),quote("NVDA")]),
        reply([quote("AAPL",110)]), requests.Timeout("sensitive error"),
        reply([quote("AAPL",130),quote("NVDA",150)]),reply([quote("AAPL",140),quote("NVDA",160)])]
    rows,_,_,_=poll(rig,["AAPL","NVDA"],duration=41)
    summary=summarize_batches(rows,["AAPL","NVDA"])
    assert summary["total_cycles"]==5
    assert summary["successful_cycles"]==3
    assert summary["network_timeouts"]==1
    assert summary["request_failure_cycles"]==1
    assert summary["instrument_failures"]==1
    assert rows[1]["batch"]["missing_symbols"]==["NVDA"]
    assert rows[2]["observations"][0]["volume_delta_status"]=="failed_sample"
    assert rows[3]["observations"][0]["volume_delta_status"]=="previous_failed_sample"
    assert rows[4]["observations"][0]["volume_delta"]==10
    assert "sensitive error" not in json.dumps(rows)


def test_rate_limit_honored_no_catchup_burst(rig):
    client,clock=rig
    limited=reply(status=429); limited.headers={"Retry-After":"25"}
    client.session.request.side_effect=[limited,reply([quote("AAPL")]),reply([quote("AAPL")])]
    rows,_,_,skipped=poll(rig,["AAPL"],duration=31)
    assert [r["actual_start_offset_seconds"] for r in rows]==[0,25,30]
    assert skipped==1
    assert summarize_batches(rows,["AAPL"])["http_429_responses"]==1


def test_rate_limit_wait_does_not_start_after_deadline(rig):
    client,_=rig
    limited=reply(status=429); limited.headers={"Retry-After":"90"}
    client.session.request.return_value=limited
    rows,_,elapsed,_=poll(rig,["AAPL"],duration=30)
    assert len(rows)==1 and elapsed==30
    assert client.session.request.call_count==1


def test_missing_future_timestamps_and_volume_reset(rig):
    client,_=rig
    client.session.request.side_effect=[reply([quote("AAPL",100,stamp=None)]),
        reply([quote("AAPL",50,stamp="2026-10-09T14:00:11Z")]),reply([quote("AAPL",55)])]
    rows,_,_,_=poll(rig,["AAPL"])
    assert rows[0]["observations"][0]["last_timestamp_status"]=="missing_timestamp"
    future=rows[1]["observations"][0]
    assert future["last_age_seconds"]==-1 and future["volume_delta"] is None
    assert future["volume_delta_status"]=="cumulative_volume_reset"
    assert rows[2]["observations"][0]["volume_delta"]==5
    ts=summarize_batches(rows,["AAPL"])["per_symbol"]["AAPL"]["timestamp_statistics"]["last"]
    assert ts["negative_age_magnitude_seconds"]["max"]==1
    assert ts["future_timestamps"]==ts["missing_timestamps"]==1


def test_volume_session_boundary_and_new_date(rig):
    client,clock=rig
    clock.base=datetime.fromisoformat("2026-10-09T19:59:55+00:00")
    client.session.request.side_effect=[reply([quote("AAPL",100)]),reply([quote("AAPL",110)]),reply([quote("AAPL",120)])]
    rows,_,_,_=poll(rig,["AAPL"])
    assert rows[1]["observations"][0]["volume_delta_status"]=="session_boundary"
    assert rows[2]["observations"][0]["volume_delta"]==10


def test_slow_requests_skip_elapsed_slots(rig):
    client,clock=rig
    def delayed(*args,**kwargs):
        clock.sleep(12)
        return reply([quote("AAPL")])
    client.session.request.side_effect=delayed
    rows,_,_,skipped=poll(rig,["AAPL"],duration=31)
    assert [r["actual_start_offset_seconds"] for r in rows]==[0,20]
    assert skipped==2


def test_reject_larger_list_before_authentication(monkeypatch):
    factory=Mock()
    monkeypatch.setattr("public_api_test.batch_stability.PublicComClient",factory)
    with pytest.raises(ValueError): run_batch_stability([f"S{i}" for i in range(21)])
    factory.assert_not_called()
    with pytest.raises(ValueError): validate_symbols(["AAPL","AAPL"])


def test_startup_auth_once_and_json_outputs(monkeypatch,tmp_path,rig):
    client,clock=rig
    client.prepare=Mock(return_value=[])
    client.session.request.return_value=reply([quote("AAPL")])
    monkeypatch.setattr("public_api_test.batch_stability.PublicComClient",lambda **kw:client)
    monkeypatch.setattr("public_api_test.batch_stability.RAW_OBSERVATIONS_DIR",tmp_path)
    monkeypatch.setattr("public_api_test.batch_stability.SUMMARY_STATS_DIR",tmp_path)
    real=monitor_batches
    def simulated(*a,**kw):
        return real(*a,**kw,clock=clock.tick,sleep=clock.sleep,wall_clock=clock.utc)
    monkeypatch.setattr("public_api_test.batch_stability.monitor_batches",simulated)
    assert run_batch_stability(["AAPL"],duration_seconds=21)
    client.prepare.assert_called_once()
    raw=[json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    summary=json.loads(next(tmp_path.glob("*.json")).read_text())
    assert len(raw)==summary["total_cycles"]==3
    assert raw[0]["batch"]["quote_request_count"]==1
    assert raw[0]["observations"][0]["last_timestamp_raw"]=="2026-10-09T14:00:00Z"
    assert raw[0]["response_received_at_utc"] is not None


def test_cli_routes_new_phase_without_batch_sweep():
    with patch("sys.argv", ["tester", "--phase", "batch_stability", "--symbols", "AAPL,NVDA", "--interval", "10", "--duration", "1800", "--requests-per-minute", "40"]),          patch("public_api_test.batch_stability.run_batch_stability", return_value=True) as run:
        from public_api_test.__main__ import main
        with pytest.raises(SystemExit) as result:
            main()
    assert result.value.code == 0
    assert run.call_args.args[:4] == (["AAPL", "NVDA"], 10, 1800, 40)

