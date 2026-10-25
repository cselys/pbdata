"""Public market-data client. Never serialize request headers or token responses."""
import os
import time
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from typing import Dict, List, Optional
import requests
from public_api_test import config
from public_api_test.models import BatchQuoteResult
from public_api_test.sessions import annotate_quote


def _safe_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class PublicComClient:
    def __init__(self, api_key=None, base_url=config.API_BASE_URL, account_id=None,
                 requests_per_minute=60, validity_in_minutes=60, token_source="auto"):
        if not 0 < requests_per_minute <= 600:
            raise ValueError("requests_per_minute must be 1..600 (Public: 10 requests/sec/account)")
        if not isinstance(validity_in_minutes, int) or isinstance(validity_in_minutes, bool) \
                or validity_in_minutes < 1:
            raise ValueError("validity_in_minutes must be a positive integer of minutes")
        if token_source not in ("auto", "fresh"):
            raise ValueError("token_source must be 'auto' or 'fresh'")
        self.api_key = api_key or os.getenv("PUBLIC_API_KEY")
        # 'fresh' never adopts an environment token: the secret is exchanged for a new one.
        self.token = None if token_source == "fresh" else os.getenv("PUBLIC_ACCESS_TOKEN")
        self.account_id = account_id or os.getenv("PUBLIC_ACCOUNT_ID")
        self.token_source = token_source
        self.validity_in_minutes = validity_in_minutes
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "public-api-tester/2.0"})
        self.spacing = 60 / requests_per_minute
        self.next_request = 0.0

    def sanitize(self, value):
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if any(t in re.sub(r"[^a-z]", "", k.lower())
                    for t in ("token", "secret", "authorization", "apikey", "cookie", "password"))
                    else self.sanitize(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(v) for v in value]
        if isinstance(value, str):
            for secret in (self.api_key, self.token, self.account_id):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", value)
        return value

    def _request(self, method, endpoint, params=None, timeout=10.0, json_body=None, auth=True):
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.response_received_at_utc = None
        start = time.perf_counter()
        self.next_request = time.monotonic() + self.spacing
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        try:
            response = self.session.request(method, self.base_url + endpoint, params=params,
                json=json_body, headers=headers, timeout=timeout, allow_redirects=False)
            self.response_received_at_utc = datetime.now(timezone.utc).isoformat()
            duration = (time.perf_counter() - start) * 1000
            try:
                body = response.json()
            except ValueError:
                body = {"raw_text": response.text}
            safe_headers = {k: self.sanitize(v) for k, v in response.headers.items()
                if k.lower() in ("content-type", "retry-after", "x-request-id", "location")
                or k.lower().startswith(("x-ratelimit-", "ratelimit-"))}
            if response.status_code == 429:
                retry = response.headers.get("Retry-After", "60")
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = 60
                self.next_request = max(self.next_request, time.monotonic() + max(1, delay))
            return response, duration, body, safe_headers
        except requests.RequestException as exc:
            # Exception strings can contain URLs/credentials; record only the class.
            return None, (time.perf_counter() - start) * 1000, {"error": type(exc).__name__}, {}

    def _record(self, symbol, endpoint, result, stage="quote", method="POST"):
        response, duration, body, headers = result
        status = response.status_code if response is not None else None
        if status is None:
            category = "network_error"
        elif status in (401, 403):
            category = "authentication_authorization_error"
        elif status in (301, 302, 303, 307, 308, 404, 405, 410, 501):
            category = "unsupported_endpoint"
        elif status == 429:
            category = "rate_limited"
        elif 200 <= status < 300:
            category = "missing_price"
        else:
            category = "api_error"
        body = self.sanitize(body)
        code = body.get("code", body.get("errorCode", body.get("error"))) if isinstance(body, dict) else None
        message = body.get("message", body.get("errorMessage")) if isinstance(body, dict) else None
        return dict(symbol=symbol, stage=stage, method=method,
            endpoint_url=self.sanitize(self.base_url + endpoint), http_status=status,
            request_duration_ms=duration, category=category, api_error_code=code,
            response_received_at_utc=getattr(self, "response_received_at_utc", None),
            api_error_message=message, price=None, provider_timestamp=None,
            raw_response=body, response_headers=headers)

    def prepare(self):
        """Authenticate and resolve a unique account; returns safe setup diagnostics."""
        records = []
        if not self.token:
            if not self.api_key:
                message = ("Fresh-token mode requires PUBLIC_API_KEY (secret); an existing "
                    "PUBLIC_ACCESS_TOKEN is not reused.") if self.token_source == "fresh" else \
                    "Set PUBLIC_API_KEY (secret) or PUBLIC_ACCESS_TOKEN locally."
                return [dict(stage="authentication", category="configuration_error",
                    api_error_message=message)]
            endpoint = "/userapiauthservice/personal/access-tokens"
            result = self._request("POST", endpoint, json_body={"secret": self.api_key,
                "validityInMinutes": self.validity_in_minutes}, auth=False)
            response, _, body, _ = result
            if response is not None and response.status_code == 200 and isinstance(body, dict):
                self.token = body.get("accessToken")
            record = self._record(None, endpoint, result, "authentication")
            # Never retain an auth body, including malformed/non-JSON responses.
            record["raw_response"] = "[authentication response omitted]"
            record["category"] = "success" if self.token else record["category"]
            records.append(record)
            if not self.token:
                return records
        if not self.account_id:
            endpoint = "/userapigateway/trading/account"
            result = self._request("GET", endpoint)
            response, _, body, _ = result
            accounts = body.get("accounts", []) if isinstance(body, dict) else []
            if response is not None and response.status_code == 200 and len(accounts) == 1:
                self.account_id = accounts[0].get("accountId")
            record = self._record(None, endpoint, result, "accounts", "GET")
            record["raw_response"] = "[account details omitted]"
            if self.account_id:
                record["category"] = "success"
            elif response is not None and response.status_code == 200:
                record.update(category="configuration_error",
                    api_error_message="Set PUBLIC_ACCOUNT_ID to select an account.")
            records.append(record)
        return records

    def get_quote(self, symbol):
        return annotate_quote(self._get_quote(symbol))

    def _get_quote(self, symbol):
        if not self.token or not self.account_id:
            records = self.prepare()
            if not self.token or not self.account_id:
                record = records[-1].copy()
                record.update(symbol=symbol, price=None, provider_timestamp=None)
                return record
        endpoint = f"/userapigateway/marketdata/{quote(self.account_id, safe='')}/quotes"
        record = self._record(symbol, endpoint, self._request("POST", endpoint,
            json_body={"instruments": [{"symbol": symbol, "type": "EQUITY"}]}))
        if record["category"] != "missing_price":
            return record
        body = record["raw_response"]
        quotes = body.get("quotes", []) if isinstance(body, dict) else []
        if not isinstance(quotes, list):
            quotes = []
        item = next((q for q in quotes if isinstance(q, dict) and
            isinstance(q.get("instrument"), dict) and q["instrument"].get("symbol") == symbol), None)
        if item is None:
            record["api_error_message"] = "Successful HTTP response has no matching quote."
            return record
        if item.get("outcome") != "SUCCESS":
            record.update(category="api_error", api_error_code=item.get("outcome"),
                api_error_message=item.get("message", "Instrument quote outcome was not SUCCESS."))
            return record
        try:
            value = float(item.get("last"))
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
            record.update(price=value, category="success")
        except (ValueError, TypeError):
            record["api_error_message"] = "Quote has no finite positive last price."
        timestamp = item.get("lastTimestamp")
        if isinstance(timestamp, str):
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    record["provider_timestamp"] = dt.timestamp()
            except ValueError:
                pass
        record.update(provider_timestamp_raw=timestamp, bid=item.get("bid"), ask=item.get("ask"),
            bid_size=item.get("bidSize"), ask_size=item.get("askSize"), volume=item.get("volume"),
            last_timestamp_raw=item.get("lastTimestamp"),
            bid_timestamp_raw=item.get("bidTimestamp"), ask_timestamp_raw=item.get("askTimestamp"))
        return record

    def get_bars_v2(self, symbol, trading_session_toggle="REGULAR_AND_EXTENDED_HOURS"):
        """DAY session-split historical bars, never substituted for a quote."""
        if trading_session_toggle not in ("REGULAR_HOURS", "REGULAR_AND_EXTENDED_HOURS", "ALL_SESSIONS"):
            raise ValueError("Invalid documented tradingSessionToggle")
        if not self.token:
            self.prepare()
        if not self.token:
            return dict(symbol=symbol, stage="historical_bars", category="configuration_error",
                http_status=None, request_duration_ms=0, raw_response={},
                api_error_message="Authentication unavailable")
        endpoint = f"/userapigateway/historicdata/EQUITY/{quote(symbol, safe='')}/DAY"
        result = self._record(symbol, endpoint, self._request("GET", endpoint,
            params={"tradingSessionToggle": trading_session_toggle}), "historical_bars", "GET")
        result.update(data_kind="historical_bars", request_parameters={"tradingSessionToggle": trading_session_toggle})
        if result["category"] == "missing_price":
            result["category"] = "success" if isinstance(result["raw_response"], dict) and any(
                k in result["raw_response"] for k in ("preMarket", "regularMarket", "afterMarket")) else "invalid_schema"
        return result

    def _sanitize_batch(self, value):
        """Batch reports omit credentials and account-specific response subtrees."""
        value = self.sanitize(value)
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if any(word in k.lower() for word in
                ("account", "balance", "position", "portfolio", "buyingpower", "email", "address", "ssn", "routing", "customer"))
                else "[non-JSON body omitted]" if k == "raw_text" else self._sanitize_batch(v))
                for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize_batch(v) for v in value]
        if isinstance(value, str) and self.account_id:
            value = value.replace(quote(self.account_id, safe=""), "[REDACTED]")
        return value

    def get_quotes_batch(self, symbols: List[str], max_batch_size=None) -> Dict:
        """One quotes POST by default; optional explicit chunk size is a local choice.

        returned_symbols counts matching unique response instruments; valid_price_count
        and completeness_ratio count successful, finite positive last prices.
        """
        symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if not symbols:
            raise ValueError("At least one symbol is required")
        if max_batch_size is not None and (not isinstance(max_batch_size, int) or max_batch_size <= 0):
            raise ValueError("max_batch_size must be a positive integer")
        setup = []
        if not self.token or not self.account_id:
            setup = self.prepare()
        results = []
        if not self.token or not self.account_id:
            # No quote request was made; distinguish setup status from quote status.
            results.append(dict(stage="setup", category="setup_failed", http_status=None,
                request_duration_ms=0, http_latency_ms=0, symbols_requested=symbols,
                returned_symbol_list=[], missing_symbols=symbols, valid_symbols=[],
                instrument_results=[], api_error_message="Authentication/account initialization failed; see setup diagnostics."))
        else:
            chunk_size = max_batch_size or len(symbols)
            endpoint = f"/userapigateway/marketdata/{quote(self.account_id, safe='')}/quotes"
            for index in range(0, len(symbols), chunk_size):
                chunk = symbols[index:index + chunk_size]
                record = self._record(None, endpoint, self._request("POST", endpoint,
                    json_body={"instruments": [{"symbol": symbol, "type": "EQUITY"} for symbol in chunk]}), "batch_quotes")
                record.update(symbols_requested=chunk, http_latency_ms=record["request_duration_ms"],
                    returned_symbol_list=[], missing_symbols=list(chunk), valid_symbols=[], instrument_results=[])
                if record["category"] == "missing_price":
                    body = record["raw_response"]
                    quotes = body.get("quotes") if isinstance(body, dict) else None
                    if not isinstance(quotes, list):
                        record.update(category="invalid_schema", api_error_message="Expected a quotes array.")
                    else:
                        matching = {}
                        duplicates, unexpected = [], []
                        for item in quotes:
                            instrument = item.get("instrument") if isinstance(item, dict) else None
                            symbol = instrument.get("symbol") if isinstance(instrument, dict) else None
                            if symbol not in chunk:
                                unexpected.append(item)
                            elif symbol in matching:
                                duplicates.append(symbol)
                            else:
                                matching[symbol] = item
                        record["returned_symbol_list"] = [s for s in chunk if s in matching]
                        record["missing_symbols"] = [s for s in chunk if s not in matching]
                        record.update(duplicate_symbols=duplicates, unexpected_quotes=unexpected)
                        for symbol in chunk:
                            item = matching.get(symbol)
                            instrument_result = dict(symbol=symbol, category="missing_symbol", price=None,
                                api_error_code=None, api_error_message="Requested instrument absent from quotes array.")
                            if item is not None:
                                instrument_result.update(outcome=item.get("outcome"),
                                    category="instrument_error", api_error_code=item.get("code", item.get("errorCode", item.get("outcome"))),
                                    api_error_message=item.get("message", item.get("errorMessage", item.get("error"))),
                                    provider_timestamp_raw=item.get("lastTimestamp"),
                                    bid_timestamp_raw=item.get("bidTimestamp"),
                                    ask_timestamp_raw=item.get("askTimestamp"),
                                    bid_price=_safe_float(item.get("bid")),
                                    ask_price=_safe_float(item.get("ask")),
                                    volume=_safe_float(item.get("volume")))
                                if item.get("outcome") == "SUCCESS":
                                    instrument_result.update(category="missing_price", api_error_code=None,
                                        api_error_message="No finite positive last price.")
                                    try:
                                        price = float(item.get("last"))
                                        if math.isfinite(price) and price > 0:
                                            instrument_result.update(price=price, category="success", api_error_message=None)
                                            record["valid_symbols"].append(symbol)
                                    except (TypeError, ValueError):
                                        pass
                            record["instrument_results"].append(instrument_result)
                        count = len(record["valid_symbols"])
                        record["category"] = "success" if count == len(chunk) else "partial_result" if count else "empty_or_failed_quotes"
                        if record["category"] != "success":
                            record["api_error_message"] = "Some requested instruments have no successful valid quote; see instrument_results."
                results.append(record)
        returned = [s for r in results for s in r["returned_symbol_list"]]
        valid = [s for r in results for s in r["valid_symbols"]]
        quote_results = [r for r in results if r.get("stage") == "batch_quotes"]
        latency = sum(r["request_duration_ms"] for r in quote_results)
        aggregate = BatchQuoteResult(len(symbols), symbols, latency, len(returned),
            len(symbols)-len(valid), len(valid)/len(symbols)).to_dict()
        aggregate.update(category=(results[0]["category"] if len(results) == 1 else
            "success" if len(valid) == len(symbols) else "partial_result" if valid else "failed"),
            http_status=quote_results[0]["http_status"] if len(quote_results) == 1 else None,
            quote_request_count=len(quote_results), returned_symbol_list=returned,
            missing_symbols=[s for s in symbols if s not in returned], valid_price_count=len(valid),
            instrument_results=[item for r in results for item in r["instrument_results"]],
            api_error_code=results[0].get("api_error_code") if len(results) == 1 else None,
            api_error_message=results[0].get("api_error_message") if len(results) == 1 else None,
            setup=setup, results=results)
        return self._sanitize_batch(aggregate)

    def get_historical(
        self,
        symbol: str,
        interval: str = "1d",
        lookback: int = 100,
    ) -> Optional[Dict]:
        """
        Get historical OHLCV data.
        
        Supported intervals: 1m, 5m, 15m, 1d (verify with actual API)
        """
        result = self._request("GET", "/v1/historical", params={"symbol": symbol, "interval": interval, "lookback": lookback})
        
        if result is None or result[0] is None or result[0].status_code != 200:
            return {
                "symbol": symbol,
                "interval": interval,
                "http_latency_ms": result[1] if result else 0,
                "candles_returned": 0,
                "maximum_available": False,
                "missing_candles": 0,
            }
        
        response, duration_ms, raw_data, response_headers = result
        
        if isinstance(raw_data, dict):
            candles_data = raw_data.get("candles", [])
            return {
                "symbol": symbol,
                "interval": interval,
                "http_latency_ms": duration_ms,
                "candles_returned": len(candles_data),
                "maximum_available": raw_data.get("maximum_available", False),
                "missing_candles": raw_data.get("missing_candles", 0),
                "extended_hours_included": raw_data.get("extended_hours_included", False),
                "timestamp_consistency": raw_data.get("timestamp_consistency", True),
                "raw_response": raw_data,
            }
        
        return {
            "symbol": symbol,
            "interval": interval,
            "http_latency_ms": duration_ms,
            "candles_returned": 0,
            "maximum_available": False,
            "missing_candles": 0,
        }

    def check_rate_limit(self) -> Dict:
        """
        Probe rate limit status.
        
        Measures successful requests per minute and captures 429 responses.
        """
        start_time = time.time()
        successful_requests = 0
        rate_limit_429_count = 0
        retry_after_headers = []
        requests_429_details = []
        total_requests = 0
        
        # Make a few test requests to observe rate limit behavior
        for i in range(5):
            result = self._request("GET", "/v1/quote", params={"symbol": "AAPL"})
            total_requests += 1
            
            if result is not None and result[0] is not None:
                if result[0].status_code == 200:
                    successful_requests += 1
                elif result[0].status_code == 429:
                    rate_limit_429_count += 1
                    retry_after = result[0].headers.get("Retry-After") if result[0] else None
                    retry_after_headers.append(retry_after)
                    requests_429_details.append({
                        "status": result[0].status_code if result[0] else 0,
                        "retry_after": retry_after,
                        "headers": {k: v for k, v in (result[3] if result else {}).items() if k.lower() not in ['authorization']},
                    })
            
            # Small delay between requests
            time.sleep(0.5)
        
        elapsed_minutes = (time.time() - start_time) / 60
        requests_per_minute = successful_requests / elapsed_minutes if elapsed_minutes > 0 else 0
        
        # Determine rate limit scope from response headers
        rate_limit_scope = "unknown"
        if result is not None and result[0] is not None:
            for key, value in result[3].items():
                key_lower = key.lower()
                if "limit" in key_lower or "rate" in key_lower:
                    rate_limit_scope = "header_derived"
                    break
        
        return {
            "successful_requests": successful_requests,
            "rate_limit_429_count": rate_limit_429_count,
            "retry_after_headers": retry_after_headers,
            "requests_per_minute": requests_per_minute,
            "rate_limit_scope": rate_limit_scope,
            "raw_headers": {k: v for k, v in (result[3] if result else {}).items() if k.lower() not in ['authorization']},
        }

    def close(self):
        """Close the HTTP session."""
        if self.session:
            self.session.close()
