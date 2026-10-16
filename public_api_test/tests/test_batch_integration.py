"""Offline batch integration tests: real client parsing, mocked HTTP transport."""
import json
from unittest.mock import Mock, patch
import pytest
import requests
from public_api_test.client import PublicComClient
from public_api_test.__main__ import main
from public_api_test.tests.test_batch import test_batch_quotes as run_batch


def response(status=200, body=None):
    result=Mock(status_code=status, headers={"Content-Type":"application/json"})
    result.json.return_value=body if body is not None else {}
    return result


def item(symbol, **extra):
    return dict(instrument={"symbol":symbol,"type":"EQUITY"}, outcome="SUCCESS",last="123.4",**extra)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("public_api_test.client.time.sleep",lambda _:None)
    c=PublicComClient(api_key="private-secret",account_id="private/account",requests_per_minute=40)
    c.token="private-token"
    c.session.request=Mock()
    yield c
    c.close()


def test_all_returned_one_quotes_request(client):
    client.session.request.return_value=response(body={"quotes":[item("NVDA"),item("AAPL")]})
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["category"] == "success"
    assert result["returned_symbols"] == result["valid_price_count"] == 2
    assert result["missing_symbols"] == []
    assert result["http_status"] == 200
    assert result["quote_request_count"] == 1
    client.session.request.assert_called_once()
    args=client.session.request.call_args.kwargs
    assert client.session.request.call_args.args[0] == "POST"
    assert client.session.request.call_args.args[1].endswith("/marketdata/private%2Faccount/quotes")
    assert args["json"] == {"instruments":[{"symbol":"AAPL","type":"EQUITY"},{"symbol":"NVDA","type":"EQUITY"}]}
    assert args["params"] is None
    assert client.spacing == 1.5
    assert result["http_latency_ms"] == result["results"][0]["request_duration_ms"]


def test_partial_and_per_instrument_errors(client):
    client.session.request.return_value=response(body={"quotes":[item("AAPL"),
        {"instrument":{"symbol":"NVDA"},"outcome":"ERROR","code":"NOT_AVAILABLE","message":"Unavailable"}]})
    result=client.get_quotes_batch(["AAPL","NVDA","MSFT"])
    assert result["category"] == "partial_result"
    assert result["returned_symbols"] == 2
    assert result["missing_symbols"] == ["MSFT"]
    assert result["partial_failures"] == 2
    assert result["completeness_ratio"] == 1/3
    assert result["instrument_results"][1]["api_error_code"] == "NOT_AVAILABLE"
    assert result["instrument_results"][2]["category"] == "missing_symbol"


@pytest.mark.parametrize("body,category",[({"quotes":[]},"empty_or_failed_quotes"), ({"unexpected":[]},"invalid_schema")])
def test_empty_and_invalid_schema(client,body,category):
    client.session.request.return_value=response(body=body)
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["category"] == category
    assert result["returned_symbols"] == 0
    assert result["http_status"] == 200
    assert result["missing_symbols"] == ["AAPL","NVDA"]


@pytest.mark.parametrize("status,category",[(401,"authentication_authorization_error"),(429,"rate_limited")])
def test_http_errors(client,status,category):
    client.session.request.return_value=response(status,{"code":"API_ERROR","message":"Request failed"})
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["http_status"] == status
    assert result["category"] == category
    assert result["api_error_code"] == "API_ERROR"
    assert result["results"][0]["raw_response"]["message"] == "Request failed"
    assert result["quote_request_count"] == 1


def test_timeout(client):
    client.session.request.side_effect=requests.Timeout("contains private-secret")
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["category"] == "network_error"
    assert result["http_status"] is None
    assert result["api_error_code"] == "Timeout"
    assert result["http_latency_ms"] >= 0
    assert "private-secret" not in json.dumps(result)


def test_authentication_and_account_initialization(client):
    client.token=client.account_id=None
    client.session.request.side_effect=[response(body={"accessToken":"new-private-token"}),
        response(body={"accounts":[{"accountId":"private-account","balance":"hidden-balance"}]}),
        response(body={"quotes":[item("AAPL"),item("NVDA")]})]
    result=client.get_quotes_batch(["AAPL","NVDA"])
    calls=client.session.request.call_args_list
    assert len(calls) == 3  # Two setup requests, exactly one quotes request.
    assert calls[0].args[1].endswith("/personal/access-tokens")
    assert calls[1].args[1].endswith("/trading/account")
    assert calls[2].args[1].endswith("/marketdata/private-account/quotes")
    assert result["category"] == "success"
    for secret in ("new-private-token","private-account","hidden-balance","private-secret"):
        assert secret not in json.dumps(result)


def test_setup_failure_sends_no_quote(client):
    client.token=client.account_id=None
    client.session.request.return_value=response(401,{"message":"bad secret"})
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["category"] == "setup_failed"
    assert result["quote_request_count"] == 0
    assert result["setup"][0]["http_status"] == 401
    assert client.session.request.call_count == 1


def test_report_redacts_credentials_and_account_data(client,monkeypatch,tmp_path,capsys):
    payload={"quotes":[item("AAPL"),item("NVDA")],
        "accountId":"other-account", "balance":"secret-balance", "email":"private-email",
        "accessToken":"unknown-token", "message":"private-secret private-token private/account"}
    client.session.request.return_value=response(body=payload)
    monkeypatch.setattr("public_api_test.tests.test_batch.PublicComClient",lambda **kw:client)
    monkeypatch.chdir(tmp_path)
    results=run_batch(["AAPL","NVDA"],[2],40)
    content=(tmp_path/"results/batch_quote_results.json").read_text()+capsys.readouterr().out
    for private in ("private-secret","private-token","private/account","private%2Faccount","other-account","secret-balance","private-email","unknown-token"):
        assert private not in content
    assert results[2]["returned_symbols"] == 2
    client.session.request.assert_called_once()


def test_cli_single_size_and_pacing(client,monkeypatch,tmp_path):
    client.session.request.return_value=response(body={"quotes":[item("AAPL"),item("NVDA")]})
    factory=Mock(return_value=client)
    monkeypatch.setattr("public_api_test.tests.test_batch.PublicComClient",factory)
    monkeypatch.chdir(tmp_path)
    with patch("sys.argv",["tester","--phase","batch","--symbols","AAPL,NVDA","--batch-sizes","2","--requests-per-minute","40"]):
        main()
    factory.assert_called_once_with(requests_per_minute=40)
    client.session.request.assert_called_once()
    assert set(json.loads((tmp_path/"results/batch_quote_results.json").read_text())) == {"2"}


def test_harness_reports_actual_failure(client,monkeypatch,tmp_path,capsys):
    client.session.request.return_value=response(429,{"code":"RATE_LIMIT","message":"Wait"})
    monkeypatch.setattr("public_api_test.tests.test_batch.PublicComClient",lambda **kw:client)
    monkeypatch.chdir(tmp_path)
    results=run_batch(["AAPL","NVDA"],[2],40)
    output=capsys.readouterr().out
    assert "No batch result returned" not in output
    assert "429" in output and "RATE_LIMIT" in output
    assert results[2]["latency_ms"] == results[2]["http_latency_ms"]


def test_no_implicit_batch_size_limit(client):
    symbols=[f"S{i}" for i in range(51)]
    client.session.request.return_value=response(body={"quotes":[item(s) for s in symbols]})
    assert client.get_quotes_batch(symbols)["returned_symbols"] == 51
    client.session.request.assert_called_once()


def test_measured_latency_not_discarded_on_failure(client):
    client.session.request.return_value=response(401,{"message":"Denied"})
    with patch("public_api_test.client.time.perf_counter",side_effect=[10.0,10.125]):
        result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["http_latency_ms"] == 125.0


def test_bad_price_and_duplicate_do_not_inflate_completeness(client):
    bad=item("NVDA"); bad["last"]="NaN"
    client.session.request.return_value=response(body={"quotes":[item("AAPL"),item("AAPL"),bad]})
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["returned_symbols"] == 2
    assert result["valid_price_count"] == 1
    assert result["results"][0]["duplicate_symbols"] == ["AAPL"]


def test_non_json_response_body_is_not_exposed(client):
    reply=response(502)
    reply.json.side_effect=ValueError()
    reply.text="arbitrary sensitive HTML content"
    client.session.request.return_value=reply
    result=client.get_quotes_batch(["AAPL","NVDA"])
    assert result["http_status"] == 502
    assert "arbitrary sensitive" not in json.dumps(result)
