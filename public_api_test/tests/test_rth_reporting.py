"""Exercise the actual CLI/report pipeline offline, not a copy of its math."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from public_api_test.__main__ import main
from public_api_test.tests import test_rth_quality as rth
from public_api_test.tests.test_freshness import monitor


def row(symbol, stamp, session="regular", volume=100, category="success"):
    return dict(symbol=symbol, price=100, bid=99, ask=101, bid_size=10, ask_size=20,
        provider_timestamp=1000, category=category, http_status=200 if category=="success" else 500,
        market_session=session, observed_at_utc=stamp, data_age_seconds=1,
        request_duration_ms=10, volume=volume, last_timestamp_status="valid")


def run_report(monkeypatch, tmp_path, rows, interrupted=False):
    class FakeClient:
        token = "mock"
        def __init__(self, **kw): pass
        def prepare(self): return []
        def close(self): pass
    monkeypatch.setattr(rth,"PublicComClient",FakeClient)
    monkeypatch.setattr(rth,"monitor",lambda *a, **kw:(rows,interrupted,2))
    monkeypatch.setattr(rth,"RAW_OBSERVATIONS_DIR",tmp_path)
    monkeypatch.setattr(rth,"SUMMARY_STATS_DIR",tmp_path)
    with patch("sys.argv",["tester","--phase","rth_quality","--symbols","AAPL,NVDA","--duration","2"]):
        if not rows or interrupted:
            with pytest.raises(SystemExit) as error: main()
            assert error.value.code == 1
        else:
            main()
    report=json.loads(next(tmp_path.glob("*.json")).read_text())
    assert [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()] == rows
    return report


def test_overall_is_sum_of_per_symbol_not_interleaved(monkeypatch,tmp_path):
    rows=[row(s,f"2026-10-08T19:59:0{i}+00:00") for i in range(3) for s in ("AAPL","NVDA")]
    report=run_report(monkeypatch,tmp_path,rows)
    assert report["frozen_timestamp_count"] == 4
    assert report["unchanged_full_response_count"] == 4
    assert report["frozen_timestamp_count"] == sum(p["frozen_timestamp_count"] for p in report["per_symbol"].values())
    assert report["total_successful"] == 6


@pytest.mark.parametrize("day,close",[("2026-10-08",20),("2026-11-27",18)])
def test_mixed_sessions_boundary_volume_and_calendar_close(monkeypatch,tmp_path,day,close):
    before=f"{day}T{close-1}:59:00+00:00"
    after=f"{day}T{close}:00:00+00:00"
    rows=[row("AAPL",before,volume=100),row("AAPL",after,"afterhours",2_000_100),
          row("AAPL",f"{day}T{close}:01:00+00:00","afterhours",2_000_150)]
    report=run_report(monkeypatch,tmp_path,rows)
    part=report["per_symbol"]["AAPL"]
    assert part["regular_session"]["observations"] == 1
    assert part["after_hours"]["observations"] == 2
    assert part["frozen_timestamp_count"] == 1  # No cross-session comparison.
    assert part["after_hours"]["frozen_timestamp_count"] == 1
    assert report["session_boundary_volume_deltas"][0]["volume_delta"] == 2_000_000
    assert len(report["large_volume_changes_near_close"]) == 1
    assert report["per_session"]["regular"]["total_requests"] == 1
    assert report["per_session"]["afterhours"]["total_requests"] == 2
    assert report["per_session"]["afterhours"]["frozen_timestamp_count"] == 1
    assert report["per_session"]["regular"]["unchanged_full_response_count"] == 0


def test_failures_break_repeats(monkeypatch,tmp_path):
    rows=[row("AAPL","2026-10-08T19:00:00+00:00"),
          row("AAPL","2026-10-08T19:00:01+00:00",category="api_error"),
          row("AAPL","2026-10-08T19:00:02+00:00")]
    report=run_report(monkeypatch,tmp_path,rows)
    assert report["frozen_timestamp_count"] == report["unchanged_full_response_count"] == 0
    assert report["total_successful"] == 2


def test_empty_run(monkeypatch,tmp_path):
    report=run_report(monkeypatch,tmp_path,[])
    assert report["total_observations"] == 0
    assert report["data_age_stats"] is None


def test_simulated_monitor_to_rth_cli(monkeypatch,tmp_path):
    start=datetime.fromisoformat("2026-10-08T15:59:59-04:00")
    elapsed=[0.0]
    class Client:
        next_request=0
        def get_quote(self,symbol):
            instant=start+timedelta(seconds=elapsed[0])
            r=row(symbol,instant.isoformat())
            r["provider_timestamp"]=instant.timestamp()
            return r
    rows,interrupted,_=monitor(Client(),["AAPL","NVDA"],1,3,lambda o:None,
        clock=lambda:elapsed[0],sleep=lambda s:elapsed.__setitem__(0,elapsed[0]+s),
        wall_clock=lambda:start+timedelta(seconds=elapsed[0]))
    report=run_report(monkeypatch,tmp_path,rows,interrupted)
    assert report["per_session"]["regular"]["total_requests"] == 2
    assert report["per_session"]["afterhours"]["total_requests"] == 4


def test_no_credentials_or_live_collection(request):
    if request.config.getoption("--run-live"):
        pytest.skip("Credential isolation applies to offline mode")
    from public_api_test.config import API_KEY
    from public_api_test.tests.test_session_bars import test_session_bars
    assert API_KEY is None
    assert test_session_bars.__test__ is False
    assert rth.test_rth_quality.__test__ is False
