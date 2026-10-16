import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from public_api_test.__main__ import main
from public_api_test.client import PublicComClient
from public_api_test.sessions import annotate_quote
from public_api_test.tests.test_freshness import monitor, summarize, format_observation, test_freshness


NOW = datetime.fromisoformat("2026-10-08T12:00:00.500000+00:00")
REGULAR_NOW = datetime.fromisoformat("2026-10-08T14:00:00.500000+00:00")
CLOSED_NOW = datetime.fromisoformat("2026-10-09T01:00:00.500000+00:00")


def observation(**extra):
    record = dict(symbol="AAPL", category="success", http_status=200, price=123,
        bid="122", ask="124", request_duration_ms=55, provider_timestamp=NOW.timestamp()-2,
        last_timestamp_raw="2026-10-08T08:00:00-04:00",
        bid_timestamp_raw="2026-10-08T11:45:00Z", ask_timestamp_raw=None)
    record.update(extra)
    return annotate_quote(record, now=NOW, clock_skew_tolerance=2)


class DataRecencyDiagnostics(unittest.TestCase):
    def annotate_closed(self, **extra):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=CLOSED_NOW.timestamp()-5,
            last_timestamp_raw=(CLOSED_NOW-timedelta(seconds=5)).isoformat(),
            bid_timestamp_raw=(CLOSED_NOW-timedelta(seconds=3)).isoformat(),
            ask_timestamp_raw=(CLOSED_NOW-timedelta(seconds=4)).isoformat())
        record.update(extra)
        return annotate_quote(record, now=CLOSED_NOW, clock_skew_tolerance=0)

    def test_recent_timestamp_during_closed_session_has_small_recency_but_is_stale(self):
        row = self.annotate_closed()
        self.assertEqual(row["market_session"], "closed")
        self.assertEqual(row["last_data_recency_seconds"], 5.0)
        self.assertEqual(row["bid_data_recency_seconds"], 3.0)
        self.assertEqual(row["ask_data_recency_seconds"], 4.0)
        self.assertEqual(row["last_timestamp_status"], "stale")
        self.assertEqual(row["last_timestamp_status"], row["bid_timestamp_status"])
        self.assertFalse(row["freshness_verified"])
        self.assertEqual(row["freshness_reason"], "provider_outside_current_session")

    def test_old_timestamp_during_closed_session_has_large_recency(self):
        row = self.annotate_closed(last_timestamp_raw=(CLOSED_NOW-timedelta(seconds=300)).isoformat(),
            provider_timestamp=(CLOSED_NOW-timedelta(seconds=300)).timestamp())
        self.assertEqual(row["last_data_recency_seconds"], 300.0)
        self.assertEqual(row["last_timestamp_status"], "stale")
        self.assertFalse(row["last_freshness_verified"])

    def test_recent_timestamp_during_regular_session_is_valid_and_fresh(self):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=REGULAR_NOW.timestamp()-2,
            last_timestamp_raw=(REGULAR_NOW-timedelta(seconds=2)).isoformat(),
            bid_timestamp_raw=(REGULAR_NOW-timedelta(seconds=1)).isoformat(),
            ask_timestamp_raw=(REGULAR_NOW-timedelta(seconds=1)).isoformat())
        row = annotate_quote(record, now=REGULAR_NOW, clock_skew_tolerance=0)
        self.assertEqual(row["market_session"], "regular")
        self.assertEqual(row["last_data_recency_seconds"], 2.0)
        self.assertEqual(row["bid_data_recency_seconds"], 1.0)
        self.assertEqual(row["ask_data_recency_seconds"], 1.0)
        self.assertEqual(row["last_timestamp_status"], "valid")
        self.assertTrue(row["last_freshness_verified"])
        self.assertEqual(row["freshness_reason"], "recent_same_session_last_trade")

    def test_future_provider_timestamp_preserves_negative_recency(self):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=(NOW+timedelta(seconds=1)).timestamp(),
            last_timestamp_raw=(NOW+timedelta(seconds=1)).isoformat(),
            bid_timestamp_raw=(NOW-timedelta(seconds=1)).isoformat(),
            ask_timestamp_raw=(NOW-timedelta(seconds=1)).isoformat())
        row = annotate_quote(record, now=NOW, clock_skew_tolerance=0)
        self.assertEqual(row["last_data_recency_seconds"], -1.0)
        self.assertEqual(row["last_timestamp_status"], "future_timestamp")
        self.assertFalse(row["last_freshness_verified"])
        self.assertEqual(row["bid_data_recency_seconds"], 1.0)

    def test_missing_timestamp_yields_null_recency(self):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=None, last_timestamp_raw=None,
            bid_timestamp_raw=None, ask_timestamp_raw=None)
        row = annotate_quote(record, now=NOW, clock_skew_tolerance=0)
        self.assertIsNone(row["last_data_recency_seconds"])
        self.assertIsNone(row["bid_data_recency_seconds"])
        self.assertIsNone(row["ask_data_recency_seconds"])
        self.assertEqual(row["last_timestamp_status"], "missing_timestamp")

    def test_invalid_timestamp_yields_null_recency(self):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=None, last_timestamp_raw="not-a-timestamp",
            bid_timestamp_raw="also-bad", ask_timestamp_raw=None)
        row = annotate_quote(record, now=NOW, clock_skew_tolerance=0)
        self.assertTrue(row["last_timestamp_parse_error"])
        self.assertIsNone(row["last_data_recency_seconds"])
        self.assertIsNone(row["bid_data_recency_seconds"])
        self.assertIsNone(row["ask_data_recency_seconds"])

    def test_last_bid_ask_calculated_independently(self):
        record = dict(symbol="AAPL", category="success", http_status=200, price=100,
            bid="99", ask="101", request_duration_ms=10,
            provider_timestamp=(REGULAR_NOW-timedelta(seconds=2)).timestamp(),
            last_timestamp_raw=(REGULAR_NOW-timedelta(seconds=2)).isoformat(),
            bid_timestamp_raw=(REGULAR_NOW-timedelta(seconds=15)).isoformat(),
            ask_timestamp_raw=(REGULAR_NOW-timedelta(seconds=45)).isoformat())
        row = annotate_quote(record, now=REGULAR_NOW, clock_skew_tolerance=0)
        self.assertEqual(row["last_data_recency_seconds"], 2.0)
        self.assertEqual(row["bid_data_recency_seconds"], 15.0)
        self.assertEqual(row["ask_data_recency_seconds"], 45.0)
        self.assertNotEqual(row["last_data_recency_seconds"], row["bid_data_recency_seconds"])
        self.assertNotEqual(row["bid_data_recency_seconds"], row["ask_data_recency_seconds"])

    def test_serialization_and_summary_compatibility(self):
        row = self.annotate_closed()
        for field in ("last_data_recency_seconds", "bid_data_recency_seconds", "ask_data_recency_seconds"):
            self.assertIn(field, row)
        serialized = json.dumps(row, default=str)
        self.assertIn("last_data_recency_seconds", serialized)
        rows = [self.annotate_closed()]
        summary = summarize(rows, ["AAPL"])
        last_stats = summary["timestamp_statistics"]["last"]
        self.assertIn("data_recency_seconds", last_stats)
        self.assertEqual(last_stats["data_recency_seconds"]["count"], 1)
        self.assertEqual(last_stats["data_recency_seconds"]["max"], 5.0)

    def test_reannotation_preserves_recency(self):
        row = self.annotate_closed()
        annotate_quote(row, now=CLOSED_NOW+timedelta(hours=1))
        self.assertEqual(row["last_data_recency_seconds"], 5.0)
        self.assertEqual(row["response_received_at_utc"], CLOSED_NOW.isoformat())
    def test_independent_ages_and_utc_conversion(self):
        row = observation()
        self.assertEqual(row["last_timestamp_raw"], "2026-10-08T08:00:00-04:00")
        self.assertEqual(row["last_timestamp_utc"], "2026-10-08T12:00:00+00:00")
        self.assertEqual(row["last_age_seconds"], .5)
        self.assertEqual(row["data_age_seconds"], row["last_age_seconds"])
        self.assertEqual(row["bid_age_seconds"], 900.5)
        self.assertEqual(row["last_timestamp_status"], "valid")
        self.assertEqual(row["bid_timestamp_status"], "stale")
        self.assertEqual(row["ask_timestamp_status"], "missing_timestamp")
        self.assertTrue(row["last_freshness_verified"])
        self.assertFalse(row["bid_freshness_verified"])

    def test_future_is_not_clamped_or_verified(self):
        row = observation(last_timestamp_raw="2026-10-08T12:00:01Z")
        self.assertEqual(row["last_age_seconds"], -.5)
        self.assertEqual(row["last_timestamp_status"], "future_timestamp")
        self.assertTrue(row["last_future_within_tolerance"])
        self.assertFalse(row["last_freshness_verified"])
        row = observation(last_timestamp_raw="2026-10-08T12:00:05Z")
        self.assertFalse(row["last_future_within_tolerance"])

    def test_malformed_and_naive_timestamps(self):
        for raw in ("bad", "2026-10-08T12:00:00"):
            row = observation(ask_timestamp_raw=raw)
            self.assertTrue(row["ask_timestamp_parse_error"])
            self.assertIsNone(row["ask_timestamp_utc"])
            self.assertIsNone(row["ask_age_seconds"])

    def test_receipt_time_is_immutable_on_reannotation(self):
        row = observation()
        annotate_quote(row, now=NOW+timedelta(hours=1))
        self.assertEqual(row["last_age_seconds"], .5)
        self.assertEqual(row["response_received_at_utc"], NOW.isoformat())
        self.assertNotEqual(row["last_age_seconds"], row["request_duration_ms"]/1000)

    def test_summary_separates_future_and_failure(self):
        rows = [observation(), observation(last_timestamp_raw="2026-10-08T12:00:01Z"),
            observation(last_timestamp_raw="2026-10-08T11:58:00Z"),
            observation(last_timestamp_raw=None, provider_timestamp=None),
            observation(category="api_error", http_status=500)]
        summary = summarize(rows, ["AAPL"])
        last = summary["timestamp_statistics"]["last"]
        self.assertEqual(last["observation_count"], 4)
        self.assertEqual(last["excluded_failed_requests"], 1)
        self.assertEqual(last["timestamp_availability"], 3)
        self.assertEqual(last["fresh_observations"], 1)
        self.assertEqual(last["stale_observations"], 1)
        self.assertEqual(last["future_timestamps"], 1)
        self.assertEqual(last["missing_timestamps"], 1)
        self.assertEqual(last["nonnegative_age_seconds"], dict(count=2,min=.5,max=120.5,mean=60.5,median=60.5,p95=120.5))
        self.assertEqual(last["negative_age_seconds"]["mean"], -.5)
        self.assertEqual(summary["per_symbol"]["AAPL"]["timestamp_statistics"]["last"], last)

    def test_client_preserves_all_provider_fields(self):
        import requests
        c = PublicComClient(account_id="test")
        c.token = "test"
        response = requests.Response(); response.status_code = 200
        payload = {"quotes": [{"instrument":{"symbol":"AAPL"}, "outcome":"SUCCESS",
            "last":"123", "lastTimestamp":"2026-10-08T12:00:00Z",
            "bid":"122", "bidTimestamp":"2026-10-08T11:45:00Z",
            "ask":"124", "askTimestamp":"2026-10-08T11:45:01Z", "volume":12345}]}
        c._request = Mock(return_value=(response,55,payload,{}))
        c.response_received_at_utc = NOW.isoformat()
        row = c.get_quote("AAPL")
        self.assertEqual(row["bid_age_seconds"],900.5)
        self.assertEqual(row["ask_age_seconds"],899.5)
        self.assertEqual(row["volume"],12345)
        self.assertEqual(row["raw_response"],payload)
        self.assertEqual(c._request.call_args.kwargs["json_body"], {"instruments":[{"symbol":"AAPL","type":"EQUITY"}]})

    def test_cli_auto_explicit_and_tolerance_compatibility(self):
        for session in (None, "auto", "premarket", "regular", "afterhours"):
            args = ["public_api_test", "--phase", "freshness", "--symbols", "AAPL,NVDA", "--duration", "12"]
            if session:
                args += ["--session", session, "--clock-skew-tolerance", "2"]
            with patch("sys.argv",args), patch("public_api_test.__main__.test_freshness.test_freshness",return_value=True) as run, contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(run.call_args.kwargs["session"],session or "auto")
            self.assertEqual(run.call_args.kwargs["clock_skew_tolerance"],2 if session else 0)

    def test_early_close_transition_and_closed_polling(self):
        for start in ("2026-11-27T12:59:59-05:00", "2026-10-08T20:00:00-04:00"):
            elapsed=[0.0]; base=datetime.fromisoformat(start)
            client=Mock(next_request=0)
            client.get_quote.side_effect=lambda symbol: dict(symbol=symbol,category="success",price=100,
                provider_timestamp=(base+timedelta(seconds=elapsed[0])).timestamp(),request_duration_ms=10,http_status=200)
            rows,_,_=monitor(client,["AAPL"],1,3,lambda o:None,
                clock=lambda:elapsed[0],sleep=lambda s:elapsed.__setitem__(0,elapsed[0]+s),
                wall_clock=lambda:base+timedelta(seconds=elapsed[0]))
            self.assertEqual(len(rows),3)
            if base.hour == 12:
                self.assertEqual([r["market_session"] for r in rows],["regular","afterhours","afterhours"])
            else:
                self.assertTrue(all(r["market_session"] == "closed" and r["last_timestamp_status"] == "stale" for r in rows))

    def test_artifacts_and_console_preserve_compatibility(self):
        rows=[observation()]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            with patch("public_api_test.tests.test_freshness.PublicComClient") as cls, \
                 patch("public_api_test.tests.test_freshness.monitor",side_effect=lambda *a, **kw: (a[4](rows[0]), (rows,False,1))[1]), \
                 patch("public_api_test.tests.test_freshness.RAW_OBSERVATIONS_DIR",path), \
                 patch("public_api_test.tests.test_freshness.SUMMARY_STATS_DIR",path), contextlib.redirect_stdout(io.StringIO()):
                cls.return_value.prepare.return_value=[]
                self.assertTrue(test_freshness(["AAPL"],duration_seconds=1))
            summary=json.loads(next(path.glob("*.json")).read_text())
            raw=json.loads(next(path.glob("*.jsonl")).read_text())
            self.assertEqual(raw["data_age_seconds"],.5)
            self.assertEqual(summary["per_session"]["premarket"]["timestamp_statistics"]["last"]["fresh_observations"],1)
        self.assertIn("bid_age=900.500s",format_observation(rows[0]))


if __name__ == "__main__":
    unittest.main()
