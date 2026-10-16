# Public.com API Real-Time Market Data Capability Tester

A standalone Python project to evaluate the market data capabilities and limitations of the Public.com API key.

## Project Structure

```
public_api_test/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── API_CAPABILITIES.md
├── FINAL_REPORT.md
└── public_api_test/
    ├── config.py
    ├── client.py
    ├── models.py
    └── tests/
        ├── test_auth.py
        ├── test_quotes.py
        ├── test_freshness.py
        ├── test_latency.py
        ├── test_rate_limits.py
        ├── test_batch.py
        ├── test_historical.py
        └── test_streaming.py
├── results/
└── tests/
```

## Overview

This project tests the Public.com API's ability to provide fresh US equity market data for a stock monitoring and intraday trading research system. **No orders are placed, no account settings are modified, and no trading operations are performed.**

### Testing Phases

1. **Authentication** - Verify API key validity and required headers
2. **Quote Testing** - Test real-time quote retrieval for sample symbols
3. **Freshness & Latency** - Measure data age and HTTP latency during market hours
4. **Rate Limit Validation** - Determine API rate limits and quotas
5. **Batch Quotes** - Test multi-symbol retrieval capability
6. **Historical Data** - Check OHLCV endpoint availability
7. **Streaming** - Test WebSocket capabilities if available
8. **Finnhub Comparison** - Compare results with Finnhub observations

### Important Notes

- API credentials are stored in environment variables, never hardcoded
- Authorization headers are never logged
- All testing is read-only (no orders, no account modifications)
- Results are saved to `results/` directory and `FINAL_REPORT.md`

### Usage

See `requirements.txt` for dependencies and setup instructions.
## Corrected quote/freshness workflow

From `C:\Git\AI\datasrc\publiccom`, store the personal secret in
`public_api_test/.env` as `PUBLIC_API_KEY`. It is exchanged for an access token;
`PUBLIC_ACCESS_TOKEN` can instead supply an existing token. Optional
`PUBLIC_ACCOUNT_ID` selects an account when more than one exists. Secrets are
never printed. Authentication and account response bodies are omitted.

```powershell
Set-Location C:\Git\AI\datasrc\publiccom
python -m public_api_test --phase diagnostic --symbols AAPL
if ($LASTEXITCODE -eq 0) {
    python -m public_api_test --phase freshness --symbols AAPL,NVDA,MSFT,TSLA,SPY --interval 5 --duration 12 --requests-per-minute 60
}
python -m unittest public_api_test.tests.test_regressions -v
```

`--interval 5` means five seconds between scheduled cycle starts. Each cycle
polls every symbol in order, with no concurrency. The local default cap is 60 requests/minute. Public documents a shared per-account
limit of 10 requests/second; this client rejects caps above 600/minute. Lower the
cap when other applications share the account. HTTP 429 honors Retry-After (seconds or date),
with a 60-second fallback. Slow cycles skip elapsed schedule slots, without
catch-up bursts. A started cycle finishes even after the duration expires.
Ctrl+C saves completed observations and marks the run interrupted.

Timestamped JSONL and summary files under results preserve older runs. Setup
requests are reported separately from market-data request counts. Success
requires a valid positive finite last price and a successful instrument outcome;
HTTP 200 alone is insufficient. Failed observations do not enter price-change,
unique-price, or data-age statistics. Missing timestamps are counted explicitly.
Data age measures time since the last trade, not transport/feed delay; unchanged
prices or old overnight trades do not prove a stale feed. HTTP timing uses a
monotonic high-resolution clock and excludes quota waiting.

Official references:
- https://public.com/api/docs/resources/market-data/get-quotes
- https://public.com/api/docs/quickstart

The repaired and verified path is diagnostic/quotes/freshness. The legacy batch,
historical, streaming and quota-probing phases have not been validated by this
investigation; use the commands above rather than `--phase all`.

## Session-aware validation

See [FINAL_REPORT.md](FINAL_REPORT.md) for the October 8 live evidence, session
capability matrix, official endpoints and exact PowerShell commands.

Install the updated requirements (including pandas-market-calendars and tzdata)
before using the new session classifier. `--session auto|premarket|regular|afterhours`
is a local validation/filter option for diagnostic, quotes and freshness. It
never changes the quote API request. Every symbol is still polled in each cycle;
raw diagnostics retain filtered observations. `--max-age 60` tests recent,
same-date, same-session last-trade evidence, not transport delay or a provider SLA.
Regular/after-hours capability must be verified during those sessions.

The new `--phase session_bars` uses the documented DAY bars endpoint independently.
`--bars-trading-session-toggle REGULAR_HOURS|REGULAR_AND_EXTENDED_HOURS|ALL_SESSIONS`
controls the documented historical parameter; its default includes extended hours.
Bars are never substituted for live quotes. Empty future-session buckets do not
mean the session is unsupported. Use this new phase instead of the legacy
`--phase historical` for session-split history.

## Independent Last / Bid / Ask timestamp diagnostics

`--session auto` remains the default. Session detection runs every cycle and every
observation; statistics split by the actual observation session, including `closed`.
Polling continues during closed hours. Explicit session options only filter local
validation; they never change the feed. Overnight standard-session labels now use
`closed`; `legacy_market_session` preserves the prior `outside_sessions` distinction.
Holiday, DST and early-close behavior continues to use the NYSE exchange calendar.

`last_age_seconds`, `bid_age_seconds`, and `ask_age_seconds` each subtract the
provider timestamp from `response_received_at_utc`, captured immediately after the
HTTP response returns and before JSON parsing. All parsed timestamps are aware UTC;
raw strings are retained in `{last,bid,ask}_timestamp_raw`, normalized values in
`{last,bid,ask}_timestamp_utc`. `data_age_seconds` remains the last-trade age.
HTTP latency is a separate monotonic duration and is never substituted for age.

Each side has `valid`, `stale`, `future_timestamp`, or `missing_timestamp` status.
Stale includes an old trade or a timestamp outside the current date/session, and
closed hours. Invalid or timezone-less timestamp strings are missing with a
separate parse-error flag; the original string is retained. No age is clamped.
`--clock-skew-tolerance 2` flags future ages within two seconds, but leaves them
future and never treats them as verified fresh. The default tolerance is zero.

New `timestamp_statistics` are available overall, per symbol, and under
`per_session`. They separate nonnegative age min/max/mean/median/P95 from negative
age statistics and count missing, stale, future, valid, and verified-fresh samples.
Failed requests are excluded and counted separately. `selected_statistics` applies
the local session filter. Legacy fields and output paths are retained; the legacy
aggregate `data_age_seconds` still includes signed ages, while the new independent
statistics explicitly split them. Fresh Last does **not** imply fresh Bid or Ask.

Short smoke test (optional live read-only requests):

```powershell
Set-Location C:\Git\AI\datasrc\publiccom
.\.venv\Scripts\python.exe -m public_api_test --phase freshness --symbols AAPL,NVDA,MSFT,TSLA,SPY --interval 5 --duration 12 --session auto --max-age 60 --clock-skew-tolerance 2 --requests-per-minute 60
```

Offline regression suite:

```powershell
.\.venv\Scripts\python.exe -m unittest public_api_test.tests.test_regressions public_api_test.tests.test_sessions public_api_test.tests.test_timestamp_diagnostics -v
```

## Default tests are offline

Run `python -m pytest public_api_test/tests/ -v` from the workspace root. The
suite requires no API key, blocks network I/O and real sleeps, and excludes all
legacy CLI `test_*` functions from collection. CLI commands remain unchanged.
The single live integration test requires explicit `--run-live`; it is skipped
by default. See FINAL_REPORT.md for the full architecture, commands, changed-file
list, RTH aggregation semantics and verification results.

## Batch quote integration (October 9, 2026)

Batch now reuses authentication/account initialization and sends an `instruments`
array to the same account-scoped POST Quotes endpoint as single-symbol Quotes.
One operation sends one Quotes request by default; no provider batch-size limit is
assumed. The optional Python `max_batch_size` argument only requests local chunking.
The CLI forwards `--requests-per-minute` to the existing client pacing mechanism.

Run exactly one two-symbol batch, without the default size sweep:

```powershell
Set-Location C:\Git\AI\datasrc\publiccom
python -m public_api_test --phase batch --symbols AAPL,NVDA --batch-sizes 2 --requests-per-minute 40
```

This sends one Quotes request after successful initialization. Token exchange and
account lookup may make additional setup requests. The command was not executed
as part of implementation. Without `--batch-sizes`, the existing 1/5/10/25/50 test
sizes remain; they are diagnostic choices, not documented provider limits.

Reports retain `results/batch_quote_results.json`, batch-size keys and the existing
latency/completeness fields. They add actual status, error category, API messages,
missing-symbol lists, instrument outcomes, sanitized JSON response data and setup
diagnostics. `returned_symbols` counts matching distinct response instruments;
`valid_price_count` counts SUCCESS instruments with finite positive last prices.
`partial_failures` is requested minus valid; `completeness_ratio` is valid/requested.
Latency is the sum of measured Quotes request times, excluding pacing and setup.
Transport failures and empty/partial responses produce a nonzero CLI exit code.
No speculative throughput estimates are derived from failed responses.
Credentials/account data are redacted; non-JSON response bodies and local exception
text are omitted. HTTP status, content type, and exception class remain diagnostic.

Offline verification: `python -m pytest public_api_test/tests/ -v` produced
**48 passed, 1 skipped, 8 subtests passed in 1.89 seconds**, without warnings.
Sixteen added batch cases cover complete/partial/empty responses, HTTP 401/429,
timeouts, initialization failure, one-request semantics, pacing propagation,
redaction, malformed responses, duplicate symbols and retained failure latency.
No live API requests were made. Live multi-symbol acceptance remains unverified
until the explicit command above is run.

## Batch Polling Stability Test

`--phase batch_stability` is separate from the Batch size sweep and existing RTH,
freshness and historical phases. It accepts 1�20 unique symbols and rejects larger
lists before authentication. Twenty is a limit of this initial experiment, not a
claim about Public's maximum supported batch size.

Authentication/account initialization runs once at startup. Each polling cycle
calls the existing `get_quotes_batch()` once without chunking. It uses the shared
client limiter, including Retry-After, without immediate retries or concurrency.
Cycles are scheduled against monotonic time; slow requests skip elapsed schedule
slots rather than drift or burst. No request starts after the monitoring deadline;
an already-started request may finish later. Token expiry/HTTP failures are recorded
without reauthenticating in the loop. Normal polling resumes on subsequent cycles.
The CLI's existing defaults remain unchanged; explicitly select 1800 seconds below.

```powershell
Set-Location C:\Git\AI\datasrc\publiccom
python -m public_api_test --phase batch_stability --symbols AAPL,NVDA,MSFT,GOOG,AMZN,META,TSLA,AVGO,AMD,NFLX,JPM,BAC,V,MA,WMT,COST,HD,UNH,XOM,CVX --interval 10 --duration 1800 --requests-per-minute 40
```

This command contains exactly 20 symbols and schedules one Quotes POST per cycle
for 30 minutes. Setup requests are additional to polling. The local pacing setting
is not an assertion about provider quotas. `--batch-sizes` is rejected for this
phase. Actual observation sessions are classified automatically; `--session` does
not select a feed or filter this stability experiment. Existing `--max-age` and
`--clock-skew-tolerance` control timestamp diagnostics.

Output paths:

- `results/raw/batch_stability_<UTC timestamp>.jsonl`: one flushed record per cycle,
  including sanitized raw batch data, HTTP metadata, returned/missing instruments,
  observed receipt time and all per-symbol observations.
- `results/summaries/batch_stability_<UTC timestamp>.json`: cycle counts, request
  failures separately from instrument failures, HTTP success/429 counts, network
  timeout/error counts, latency mean/P50/P95/max, actual intervals and skipped
  schedule slots, and per-symbol timestamp/volume metrics.

Each symbol retains original Last/Bid/Ask timestamps, parsed UTC values, signed
ages, future-timestamp counts and negative-age magnitudes. Age is measured from
HTTP receipt, independently of request latency. A failed request has no actual HTTP
receipt time: `http_response_received_at_utc` is null and `receive_time_source`
identifies the failure-completion fallback used for session classification.
Cumulative volume is preserved as returned; finite nonnegative values are used for
deltas. Deltas never bridge failed samples, session/date changes, missing volume or
resets. Invalid intervals have null deltas and explicit reasons; after a reset or
failure, the next valid sample establishes a new baseline. A reset sample itself
can be the baseline for the following same-session interval.

Successful cycles require valid prices for every requested symbol; this does not
prove freshness of every side. The report's freshness statistics stay separate.
Request errors do not also inflate individual instrument failure counts. Failures
do not stop subsequent cycles. A completed run with any failed/partial cycle is
`degraded` with a nonzero exit code; Ctrl+C saves completed cycles and reports
`interrupted`. Credentials and account identifiers are sanitized before persistence.

Implementation verification: **60 passed, 1 skipped, 8 subtests passed in 1.44s**
using `python -m pytest public_api_test/tests/ -v`, with no warnings. Twelve added
stability tests cover 20-symbol one-request cycles, startup setup, partial responses,
timeouts/connections/client errors, HTTP 429/deadlines, missing/future timestamps,
volume resets/session/date boundaries, slow requests, CLI routing and JSON outputs.
HTTP transport and time were simulated; no live calls were made. The live command
above has not been executed by the implementation agent.

## Phase 2A: offline volume spike detector

Run from the project root (no credentials or network required):

```powershell
python -m public_api_test.volume_spikes --input results\raw\multi_batch_stability_20261012T141239357397Z.jsonl --threshold 3.0 --window 30 --min-samples 10 --max-age 60 --max-gap 30
```

Outputs go to `results/volume_spikes` (override with `--output-dir`): JSON candidates,
summary counts for every symbol, and an audit row for every observed sample; CSV
candidates sorted by descending RVOL. Input SHA-256 is recorded. Raw provider
payloads, account identifiers and credentials are not copied into reports.

Each interval uses the symbol's actual UTC receive times. Rate is cumulative-volume
delta divided by elapsed seconds. The rolling baseline is the sum of prior volume
deltas divided by their total elapsed seconds, limited to `--window` intervals.
The current interval enters the baseline only AFTER evaluation. At least
`--min-samples` prior intervals are required; threshold comparison is inclusive.
Zero deltas are valid samples. A zero baseline leaves RVOL undefined and produces
no candidate. Missing/failed/stale samples, invalid timestamps/volumes, session or
local-date changes, non-increasing time, gaps above `--max-gap`, and negative volume
corrections break continuity and restart warmup. No missing delta is imputed.

Freshness uses last-trade age, with future timestamps rejected and a configurable
maximum age. This is only a proxy: cumulative volume has no separate update timestamp.
Session labels come from the collector; New York local date handles overnight
boundaries. Files are processed in recorded order, without sorting or future data.
Malformed JSON/schema or duplicate symbols in a sweep stop processing explicitly.
Candidate counts are interval counts, without event deduplication or a minimum-share
filter. Historical baseline is local to this recording, not time-of-day seasonality.

## Phase 2B: offline signal quality validation

```powershell
python -m public_api_test.volume_quality --input results\raw\multi_batch_stability_20261012T141239357397Z.jsonl --output-dir results\phase2b --event-gap 30 --cooldown 120 --min-samples 10 --max-age 60 --max-gap 30 --horizon-tolerance 15 --match-lookback 600 --match-rate-factor 2
python -m pytest public_api_test/tests/ -v
```

The standalone module imports the unchanged Phase 2A detector and never imports
collector/client/authentication code. It compares RVOL 2/3/4/5 and windows 30/60/90,
with both a fixed minimum sample count and full-window warmup. Primary event and
price analysis uses RVOL 3, window 30 and the configured minimum samples.

An event begins at the first candidate's receive time. Candidates within
`--event-gap` seconds of the previous candidate join that event. Quality breaks
split events. A new event inside `--cooldown` seconds of the previous alert-eligible
start is recorded but suppressed as an alert opportunity. These are simulated
opportunities only; no alerts are sent. End/peak/aggregate metrics are retrospective;
right-censored clusters may extend beyond the recording.

Forward returns use actual receipt times and the first observation at/after the
horizon (maximum overshoot `--horizon-tolerance`). Controls are earlier same-symbol,
same-session non-events, with similar baseline volume rates, completed follow-up
before decision time, no nearby candidates, and no reuse/overlap. Missing paths,
matching losses and separate-event contamination are explicit. See the generated
report for complete matching rules and limitations.

Outputs in `results/phase2b`:
- `events.json` / `events.csv`: event descriptors and quality diagnostics (JSON).
- `sensitivity.json` / `sensitivity.csv`: 24 configurations, eligibility/warmup
  denominators, and common-eligible candidate counts. JSON includes per-symbol counts.
- `price_response.json` / `price_response.csv`: forward returns, sampled up/down
  excursions, missing reasons, matched controls and paired summaries.
- `per_symbol.csv`: primary candidate/event/alert-opportunity distribution.
- `PHASE_2B_REPORT.md`: human-readable methods, results and readiness assessment.

This is descriptive offline validation, not predictive-performance evidence.
The raw recording remains unchanged. Output reports omit account and credential
fields; output directories should be distinct when retaining multiple analyses.

## Phase 3: instrumented live shadow monitoring

Use the new standalone `python -m public_api_test.shadow` CLI. `--mode replay`
performs offline deterministic validation; `--mode live` explicitly enables the
existing authenticated batch collector. No alerts or orders are sent.

See [SHADOW_MONITORING.md](SHADOW_MONITORING.md) for exact replay/live/resume
commands, output schemas, checkpoint recovery, timing semantics and limitations.
Defaults: 100 configured stocks, 20 symbols/request, 10-second sweeps, 40 RPM,
RVOL 3, window 30, minimum 10 samples, 30-second event gap, 120-second cooldown.
The Phase 2A and Phase 2B CLIs retain their existing behavior through shared engines.

Session-end summaries now identify incomplete sampling coverage and write
`session_end.json` / `SESSION_END_REPORT.md`. For a read-only report of an existing
session, use `python -m public_api_test.shadow_report --session <session-directory>
--output-dir <separate-report-directory>`. See SHADOW_MONITORING.md for scope and schema.
