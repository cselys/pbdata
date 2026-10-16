import unittest
from unittest.mock import Mock
import requests
from public_api_test.client import PublicComClient
from public_api_test.tests.test_freshness import monitor, summarize

class Regression(unittest.TestCase):
    def client(self, status, body):
        c=PublicComClient(api_key='private-secret', account_id='account')
        c.token='private-token'
        response=requests.Response(); response.status_code=status
        c._request=Mock(return_value=(response, 100, body, {}))
        return c
    def test_schema(self):
        c=self.client(200, {'quotes':[{'instrument':{'symbol':'AAPL'}, 'outcome':'SUCCESS', 'last':'123.45','lastTimestamp':'2026-10-08T04:55:51Z'}]})
        r=c.get_quote('AAPL')
        self.assertEqual(r['price'],123.45); self.assertIsNotNone(r['provider_timestamp'])
        self.assertEqual(c._request.call_args.args[0],'POST')
    def test_failures(self):
        for status, expected in [(401,'authentication_authorization_error'),(403,'authentication_authorization_error'),(404,'unsupported_endpoint'),(429,'rate_limited'),(500,'api_error'),(200,'missing_price')]:
            r=self.client(status, {'message':'failed'}).get_quote('AAPL')
            self.assertEqual(r['http_status'],status); self.assertEqual(r['category'],expected)
            self.assertIsNone(r['price'])
    def test_network(self):
        c=self.client(200,{})
        c._request=Mock(return_value=(None,20,{'error':'ConnectionError'},{}))
        self.assertEqual(c.get_quote('AAPL')['category'],'network_error')
    def test_redaction(self):
        c=self.client(200,{})
        self.assertEqual(c.sanitize({'accessToken':'anything','message':'private-secret private-token'}),{'accessToken':'[REDACTED]','message':'[REDACTED] [REDACTED]'})
    def test_cycle_and_statistics(self):
        now=[0.0]; calls=[]
        class Fake:
            next_request=0
            def get_quote(self,s):
                calls.append(s)
                now[0]+=.1
                return dict(symbol=s,category='success' if s!='NVDA' else 'api_error',price=123 if s!='NVDA' else None,provider_timestamp=None,http_status=200 if s!='NVDA' else 500,request_duration_ms=100)
        symbols=['AAPL','NVDA','MSFT','TSLA','SPY']
        obs, interrupted, elapsed=monitor(Fake(),symbols,5,11,lambda o:None,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s))
        self.assertEqual(calls,symbols*3)
        self.assertEqual(obs[5]['sampling_interval_seconds'],5)
        summary=summarize(obs,symbols)
        self.assertEqual(summary['total_requests'],15)
        self.assertEqual(summary['valid_price_count'],12)
        self.assertEqual(summary['failed_requests'],3)
        self.assertEqual(summary['per_symbol']['NVDA']['unique_prices'],0)
    def test_invalid_last(self):
        for price in [None,'NaN','Infinity','oops','0']:
            c=self.client(200,{'quotes':[{'instrument':{'symbol':'AAPL'},'outcome':'SUCCESS','last':price}]})
            self.assertEqual(c.get_quote('AAPL')['category'],'missing_price')

if __name__=='__main__':
    unittest.main()
