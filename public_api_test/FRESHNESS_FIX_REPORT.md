# Freshness investigation — October 8, 2026

## Root cause

The original GET https://api.public.com/v1/quote?symbol=AAPL returns HTTP 302,
an empty body, and Location https://public.com/api/docs. requests previously
followed redirects automatically; the saved HTTP 200 observations therefore
were not evidence of valid quote responses. The documentation page is HTML.
The original raw freshness output discarded status/body, so the precise original
response cannot be recovered retrospectively.

The documented quote request is POST
/userapigateway/marketdata/{accountId}/quotes with instruments containing symbol
and type EQUITY. Its response is quotes[].last and ISO-8601 lastTimestamp, not
top-level price and numeric timestamp. The secret must be exchanged for an
access token first. The old code used the secret directly as a bearer token.
The response truthiness check also hid real 4xx/5xx statuses as zero.

The monitor advanced its shared deadline inside the symbol loop, immediately
after AAPL. Remaining symbols failed that deadline check every cycle.

## Verification

A single AAPL diagnostic successfully exchanged the secret, resolved the account,
and obtained HTTP 200, last 336.20, lastTimestamp 2026-10-08T04:55:51Z.
Authentication and market-data permission both work with the current credential;
quote capability is supported. No permission change is needed.

A 12-second scheduled test completed its final cycle in approximately 15 seconds:
15 requests, all HTTP 200, all valid prices with timestamps, three observations
per symbol, five-second sampling intervals. Quote latency: 31–78 ms, mean 46.93 ms
in that run (subsequently upgraded timing to perf_counter for finer resolution).
Last-trade ages ranged from about 5 to 2932 seconds during overnight trading;
this validates quote retrieval, not regular-market feed freshness.

Six offline regression tests passed: documented parsing, HTTP failure categories,
network failure, secret redaction, all-symbol cycles/statistical exclusion, and
invalid prices. No 300-second run or trading operation was executed.

## Modified files

- client.py: token exchange, account lookup, documented quote request/schema,
  diagnostics, credential redaction, pacing, Retry-After, disabled redirects.
- config.py: deterministic .env discovery; no import-time credential exception.
- __main__.py: diagnostic phase, request cap, 15-second default, failing exit codes.
- tests/test_freshness.py: monotonic complete cycles, measurements and persistence.
- tests/test_quotes.py and tests/test_auth.py: valid-quote success criteria.
- tests/test_regressions.py: offline regression coverage.
- legacy_diagnostic.py: safe reproduction of obsolete endpoint response.
- README.md and this report: commands, semantics and scope.

See README.md for exact PowerShell commands. Historical/batch/streaming legacy
phases remain outside the verified freshness path.
