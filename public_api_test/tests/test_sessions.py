import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import requests
from public_api_test.sessions import session_at, annotate_quote, quote_capabilities
from public_api_test.client import PublicComClient
from public_api_test.tests.test_session_bars import parse_bars
from public_api_test.tests.test_freshness import monitor


def dt(value):
    return datetime.fromisoformat(value)


class Sessions(unittest.TestCase):
    def test_boundaries_and_dst(self):
        for value, expected in [
            ("2026-03-06T14:29:59+00:00", "premarket"),
            ("2026-03-06T14:30:00+00:00", "regular"),
            ("2026-03-09T13:30:00+00:00", "regular"),
            ("2026-11-02T14:30:00+00:00", "regular"),
            ("2026-10-08T04:00:00-04:00", "premarket"),
            ("2026-10-08T09:30:00-04:00", "regular"),
            ("2026-10-08T16:00:00-04:00", "afterhours"),
            ("2026-10-08T20:00:00-04:00", "closed"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(session_at(dt(value)), expected)

    def test_holidays_weekend_early_close(self):
        for day in ["2026-01-01", "2026-04-03", "2026-06-19", "2026-07-03",
                    "2026-11-26", "2026-12-25", "2026-10-10", "2025-01-09"]:
            self.assertEqual(session_at(dt(day + "T12:00:00-04:00")), "closed")
        self.assertEqual(session_at(dt("2026-11-27T12:59:00-05:00")), "regular")
        self.assertEqual(session_at(dt("2026-11-27T13:00:00-05:00")), "afterhours")
        self.assertEqual(session_at(dt("2026-11-27T17:00:00-05:00")), "closed")

    def test_last_trade_freshness_and_filter(self):
        now = dt("2026-10-08T08:00:00-04:00")
        base = dict(symbol="AAPL", category="success", price=123, http_status=200,
                    raw_response={"marketSession": "PRE_MARKET"}, endpoint_url="quotes")
        for age, expected in [(10, True), (61, False), (-1, False), (86400, False)]:
            r = annotate_quote(dict(base, provider_timestamp=(now-timedelta(seconds=age)).timestamp()), now=now)
            self.assertEqual(r["freshness_verified"], expected)
            self.assertEqual(r["api_session_indicators"], {"marketSession": "PRE_MARKET"})
        r = annotate_quote(dict(base, provider_timestamp=now.timestamp()), "regular", now=now)
        self.assertFalse(r["session_filter_match"])
        self.assertFalse(r["freshness_verified"])
        self.assertIsNone(quote_capabilities([r], ["AAPL"])["regular"]["accessible_with_key"])
        r = annotate_quote(dict(base, provider_timestamp=None), now=now)
        self.assertFalse(r["freshness_verified"])

    def test_bars_remain_historical_and_preserve_bucket(self):
        r = dict(symbol="AAPL", category="success", http_status=200, request_duration_ms=10,
                 endpoint_url="bars", request_parameters={}, raw_response={
                    "preMarket": {"bars": [{"timestamp":"2026-10-08T08:00:00-04:00", "close":"123"}]},
                    "afterMarket": {"bars": [{"timestamp":"bad", "close":"NaN"}]}})
        rows = parse_bars(r, "premarket", dt("2026-10-08T08:00:10-04:00"))
        self.assertEqual(rows[0]["price"], 123)
        self.assertFalse(rows[0]["freshness_verified"])
        self.assertTrue(rows[0]["session_filter_match"])
        self.assertFalse(rows[1]["session_filter_match"])
        self.assertIsNone(rows[1]["price"])
        self.assertIsNone(rows[1]["provider_timestamp"])

    def test_endpoint_and_quota_cap(self):
        with self.assertRaises(ValueError):
            PublicComClient(requests_per_minute=601)
        c = PublicComClient(account_id="test")
        c.token = "test"
        response = requests.Response(); response.status_code=200
        c._request = Mock(return_value=(response, 1, {"preMarket": {"bars": []}}, {}))
        c.get_bars_v2("AAPL", "REGULAR_HOURS")
        self.assertEqual(c._request.call_args.args, ("GET", "/userapigateway/historicdata/EQUITY/AAPL/DAY"))
        self.assertEqual(c._request.call_args.kwargs["params"], {"tradingSessionToggle":"REGULAR_HOURS"})

    def test_pacing_preserves_cycles_and_session_transition(self):
        elapsed=[0.0]; calls=[]
        start = dt("2026-10-08T09:29:58-04:00")
        class Fake:
            next_request=0
            def get_quote(self, symbol):
                calls.append(symbol)
                self.next_request=elapsed[0]+1
                return dict(symbol=symbol,category="success",price=100,http_status=200,
                    request_duration_ms=10,provider_timestamp=(start+timedelta(seconds=elapsed[0])).timestamp())
        rows, _, _ = monitor(Fake(), ["AAPL","NVDA","MSFT","TSLA","SPY"],5,6,lambda o:None,
            clock=lambda:elapsed[0],sleep=lambda s:elapsed.__setitem__(0,elapsed[0]+s),
            session="regular",wall_clock=lambda:start+timedelta(seconds=elapsed[0]))
        self.assertEqual(calls,["AAPL","NVDA","MSFT","TSLA","SPY"]*2)
        self.assertFalse(rows[0]["session_filter_match"])
        self.assertTrue(rows[2]["session_filter_match"])
        self.assertEqual(rows[5]["sampling_interval_seconds"],5)

    def test_retry_after(self):
        c = PublicComClient(account_id="test")
        response = requests.Response(); response.status_code=429
        response.headers["Retry-After"]="20"
        response._content=b'{"error":"rate limit"}'
        c.session.request=Mock(return_value=response)
        with patch("public_api_test.client.time.monotonic",return_value=100), patch("public_api_test.client.time.sleep"):
            c._request("GET","/example")
        self.assertEqual(c.next_request,120)


if __name__ == "__main__":
    unittest.main()
