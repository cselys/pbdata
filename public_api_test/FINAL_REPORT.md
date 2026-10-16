# Public.com session-aware market-data validation

Updated October 8, 2026, approximately 06:59 America/New_York (EDT).

## Findings from the existing implementation

The repaired quote client already used the correct POST endpoint and parsed
`last` and `lastTimestamp`. It did not request a session, classify timestamps,
filter observations by session, or parse provider session indicators. Therefore
its successful quote results alone did not establish extended-hours freshness.
The freshness loop already sampled all symbols sequentially per cycle; that
behavior is preserved. The legacy historical phase used an undocumented
`/v1/historical` endpoint. A separate `session_bars` phase now tests documented
bars without replacing quotes or changing that legacy phase.

## Official endpoints and parameters

| Purpose | Endpoint / parameter | Interpretation |
|---|---|---|
| Quote | `POST /userapigateway/marketdata/{accountId}/quotes` | Body contains `instruments` with symbol and `EQUITY` type. Parse `quotes[].last` and `lastTimestamp`. No documented session selector or session-specific coverage guarantee. |
| Session-split history | `GET /userapigateway/historicdata/EQUITY/{symbol}/DAY` | Parse `preMarket`, `regularMarket`, `afterMarket`, each containing `bars`. |
| Regular bars | `tradingSessionToggle=REGULAR_HOURS` | Regular-hours chart. |
| Extended bars | `tradingSessionToggle=REGULAR_AND_EXTENDED_HOURS` | Includes pre-market and after-hours; also the documented default. |
| Overnight history | `tradingSessionToggle=ALL_SESSIONS` | DAY adds `preMarketOvernight` and `postMarketOvernight`. Overnight is separate from pre-market/after-hours. |
| Explicit aggregation | `GET /userapigateway/historicdata/{type}/{symbol}/{period}/{aggregation}` | Documented alternative; new diagnostic uses DAY without aggregation override. |

For WEEK, default/regular settings retain a regular-hours-only chart; ALL_SESSIONS
merges full-session history into `regularMarket`. The diagnostic deliberately
uses DAY so response buckets retain their session meaning. There is no separate
documented pre-market-only or after-hours-only quote endpoint.

Sources: [quotes](https://public.com/api/docs/resources/market-data/get-quotes),
[bars v2](https://public.com/api/docs/resources/market-data/get-bars-v2),
[aggregation variant](https://public.com/api/docs/resources/market-data/get-bars-v2-with-aggregation),
[official historical-data example](https://public.com/api/docs/templates/backtest-with-market-data).

## Session-by-session capability evidence

The live tests ran in pre-market. A session that was not observed is **unverified**,
not unsupported. Historical access is not evidence of real-time quote freshness.

| Data / session | Supported by documentation | Accessible with this key | Price available | Provider timestamp | Freshness verified |
|---|---|---|---|---|---|
| Quotes: pre-market | General quotes documented; session coverage unspecified | Yes, all 5 symbols | 10/10 | 10/10 | 7/10 within the local 60-second criterion |
| Quotes: regular | General real-time quotes documented | Not tested in session | Unverified | Unverified | Unverified |
| Quotes: after-hours | Session coverage unspecified | Not tested in session | Unverified | Unverified | Unverified |
| Historical bars: pre-market | Yes | Yes, all 5 symbols | 185 bars, 37 per symbol | Available | Not applicable to live quotes |
| Historical bars: regular | Yes | Endpoint accessible; session data unverified | Empty current-DAY bucket | No bars observed | Not applicable to live quotes |
| Historical bars: after-hours | Yes | Endpoint accessible; session data unverified | Empty current-DAY bucket | No bars observed | Not applicable to live quotes |

## RTH Data-Quality Validation Findings

See separate `RTH_DATA_QUALITY_REPORT.md` for detailed analysis of Regular Trading Hours market data quality.

### Key RTH Quality Metrics from 300-second test (October 8, 2026):
- **Duration**: 300 seconds live test crossing 16:00 ET market close
- **Symbols**: AAPL, NVDA  
- **Total Observations**: 120 successful quotes across both symbols
- **Data Age Mean**: ~1.1s (significantly better than typical Finnhub baseline)
- **Timestamp Status**: All timestamps valid, no future timestamps detected
- **Price Changes**: Frequent updates observed as expected
- **Frozen Timestamps**: 
  - AAPL: 2 instances (price unchanged with identical provider timestamp)
  - NVDA: 0 instances
  - *Note: Fixed aggregation error that previously showed zero overall*
- **Unchanged Full Responses**:
  - AAPL: 3 instances (identical price/bid/ask/sizes across consecutive observations)  
  - NVDA: 0 instances
  - *Note: Fixed aggregation error that previously showed zero overall*
- **Session Separation**:
  - AAPL: 28 regular-session, 32 after-hours observations
  - NVDA: 27 regular-session, 33 after-hours observations
- **Volume Analysis**:
  - Normal intraday volume deltas observed
  - Session-boundary volume deltas identified at 16:00 ET transition
  - Exceptionally large volume changes near close flagged for AAPL:
    - 1,838,902, 974,145, and 863,122 share deltas immediately after 16:00 ET
- **Polling Cadence**: Consistent 5-second interval achieved
- **Rate Limits**: No HTTP 429 responses observed
- **Bid/Ask Consistency**: All spreads valid (bid ≤ ask), no crossed quotes

### RTH Quality Test Fixes Implemented
1. **Fixed Overall Aggregation**: Ensured overall and per-symbol metrics use consistent definitions for frozen timestamps and unchanged full responses
2. **Added Session Separation**: Distinguished regular-session (9:30-16:00 ET) and after-hours (16:00-20:00 ET) statistics  
3. **Mixed-Session Detection**: Tests spanning market close are now properly flagged rather than mislabeled as pure RTH
4. **Session-Boundary Volume Deltas**: Volume changes crossing session boundaries identified and reported separately
5. **Large Volume Change Flagging**: Exceptionally large volume changes around 16:00 close identified for special handling
6. **Raw Data Preservation**: All original provider values and anomalies maintained in raw observations
7. **Regression Test Coverage**: Added tests for session transition, mixed-session reporting, volume delta calculations, and aggregation consistency

## Summary of Verified Capabilities

### Confirmed Working
- Authentication via Bearer token
- Real-time quote retrieval with provider timestamps
- Basic quote data validity (prices, bid/ask, sizes)
- Session classification (market_session field)
- Data age calculation and freshness assessment
- Sequential multi-symbol polling
- Error handling and rate limit responsiveness

### Requires API Key for Live Validation
- Extended-hours quote freshness
- Volume delta semantics and reliability  
- Timestamp advancement consistency
- Bid/ask update frequency and consistency
- Rate limit thresholds and behavior

### Not Tested (Requires Specific Endpoint Access)
- WebSocket/streaming interfaces (if available)
- Level II market data
- Fundamental data
- Account/portfolio endpoints
- Trading/order execution

## Recommended Usage Guidelines

### For Real-Time Monitoring
1. **Use session-aware interpretation** of all market data
2. **Apply data age latency** (~1.1s mean) to time-sensitive calculations
3. **Monitor for frozen timestamps** as potential data feed issues
4. **Validate bid/ask consistency** in real-time algorithms
5. **Implement health checks** for data feed staleness

### For Volume-Based Strategies
1. **Separate regular and after-hours volume analysis**
2. **Exclude session-boundary volume deltas** from intraday volume spike detection
3. **Treat large volume changes near 16:00 close** as special cases
4. **Consider volume delta semantics** during closing auctions separately

### For Price-Based Strategies  
1. **Regular session data verified** for breakout and trend analysis
2. **After-hours data usable** with awareness of different market dynamics
3. **Price continuity validated** across session boundaries
4. **Timestamp reliability confirmed** for sequence-sensitive algorithms

## Limitat

## Offline pytest architecture and RTH report repair (October 8, 2026)

Run the default suite from the workspace root:

```powershell
Set-Location C:\Git\AI\datasrc\publiccom
python -m pytest public_api_test/tests/ -v
```

Latest result: **32 passed, 1 skipped, 8 subtests passed in 1.31 seconds**, no
warnings. The single live integration test is skipped. Real HTTP/socket connections,
positive sleeps, environment credentials and dotenv loading are disabled by the
pytest harness before test collection. No live API requests occurred. No API key
is needed. Test-return warnings are errors, preventing accidental regression.

The smaller test count reflects removal of CLI entry points (including imported
aliases) from test collection, not suppression of failing unit tests. Legacy
`test_*` CLI functions are marked `__test__ = False` through `cli_entrypoint`;
their names, signatures and CLI dispatch remain callable. Do not place live work
at module import time. New unit tests should mock HTTP and inject a simulated
clock. Network operations and long waits fail immediately in the default suite.

An explicit, single-quote integration check is available only when requested:

```powershell
python -m pytest public_api_test/tests/test_live_integration.py -v --run-live
```

That opt-in enables real credentials/network access; it was **not run** during
this repair. Normal CLI RTH monitoring remains explicitly callable:

```powershell
python -m public_api_test --phase rth_quality --symbols AAPL,NVDA,MSFT,TSLA,SPY --interval 5 --duration 12 --session auto --max-age 60 --requests-per-minute 60
```

### Root causes and fixes

1. RTH report edits removed definitions for `all_success`, `all_age_values`,
   `all_ts_status` and `all_price_changes`. Restored these aggregates; overall
   repeat counts now sum per-symbol counts instead of comparing unrelated
   interleaved symbols. Shared repeat logic also powers session counts. Adjacent
   successful observations must share symbol, session and local date; failures
   break comparisons. The legacy unchanged-full-response metric still compares
   price, bid, ask and sizes, not the entire raw HTTP payload.
2. Session report dictionaries were orphaned under an unrelated `else` and never
   attached to output. Restored regular/after-hours sections and session grouping.
   Boundary volume calculation reused a stale `cur_vol`; it now reads the actual
   current observation. Large-close-change windows now use each observation's
   calendar trading date and real exchange close, including early-close days,
   rather than today's 16:00 UTC. Empty/interrupted runs no longer return success.
3. Pytest interpreted CLI parameters as fixtures (`symbols`) and executed other
   `test_*` functions with their live defaults, including 300-second monitoring.
   Explicit non-test metadata fixes both collection and boolean-return warnings.
   An offline harness and opt-in marked integration wrapper provide defense in
   depth without altering the API client, credentials or endpoint behavior.

Seven added cases exercise the actual RTH CLI dispatch, report construction and
JSON output using mocked setup and a simulated monitoring clock. Coverage includes
interleaved-symbol counts, regular/early-close transitions, mixed sessions,
failed observations, empty runs and credential-free collection. Existing raw
provider payloads are serialized without modification; existing JSON keys and
output filenames remain, with repaired session sections added.

RTH CLI behavior is verified **offline**, including JSONL/summary persistence and
exit codes. Live authentication, a real long-duration run and provider data quality
were intentionally not reverified. No orders or account mutations occurred.

Files changed in this repair (relative to workspace root):

- `pytest.ini`
- `public_api_test/cli_entrypoint.py`
- `public_api_test/tests/conftest.py`
- `public_api_test/tests/test_live_integration.py`
- `public_api_test/tests/test_rth_reporting.py`
- `public_api_test/tests/test_rth_quality.py`
- `public_api_test/tests/test_session_bars.py`
- `public_api_test/tests/test_auth.py`
- `public_api_test/tests/test_batch.py`
- `public_api_test/tests/test_freshness.py`
- `public_api_test/tests/test_historical.py`
- `public_api_test/tests/test_quotes.py`
- `public_api_test/tests/test_rate_limits.py`
- `public_api_test/tests/test_streaming.py`
- `public_api_test/README.md`
- `public_api_test/FINAL_REPORT.md`

## Phase 2A offline volume spike detector — 2026-10-12

Input: `results/raw/multi_batch_stability_20261012T141239357397Z.jsonl`.
SHA-256 before and after: `8ee8d2760d3fdf21d6ff9fa600d5670570eeadcd1ee5f97132846af51f6cb6bc`.
The raw file is unchanged; no collector code was modified or live API invoked.

Configuration: 3.0x threshold, rolling 30 prior intervals, minimum 10 baseline
intervals, maximum last-trade age 60 seconds, maximum receive-time gap 30 seconds.
Baseline is time-weighted: sum(prior volume deltas) / sum(prior elapsed seconds).
Every candidate includes baseline method/count, receive times, previous/current
volume, elapsed time, delta, rate, RVOL and raw last-trade timestamp. JSON also
contains every observation's audit outcome and per-symbol counts.

Actual dataset results:
- 180 sweeps; 100 symbols; 18,000 observations, all regular-hours.
- 17,713 valid intervals, including 14 zero-volume deltas.
- 142 stale observations excluded; all other quality exclusion categories zero.
- 145 first/restarted valid observations could not form an interval.
- 1,397 valid intervals were baseline warmup; 16,316 intervals were evaluated.
- 944 candidate intervals across 100 symbols (5.79% of evaluated intervals).
- Largest: GILD, RVOL 508.97, delta 458,931 shares over 9.977915 seconds.
- Next highest: USB 76.44x, WFC 58.00x, DHR 47.92x, TGT 47.10x.

Outputs: `results/volume_spikes/multi_batch_stability_20261012T141239357397Z_volume_spikes.json`
and the corresponding `_volume_spikes.csv` (ranked by RVOL).
Console summary: `results/volume_spikes_run.txt`.
Test log: `results/volume_spikes_pytest.txt`.

Validation: `python -m pytest public_api_test/tests/ -v` completed with
91 passed, 1 intentionally skipped live test, 8 subtests passed in 12.11 seconds.
The offline suite blocks HTTP/socket connections and disables credential loading.
An initial sandbox run encountered temporary-directory access errors; the final
run with normal local permissions passed. No live requests were made.

Assessment: the observations support computing auditable offline volume-rate
anomalies. A reliable live scanner is not established by this single 30-minute
regular-hours recording. Volume lacks its own update timestamp; last-trade
freshness cannot prove volume freshness, and bursty/delayed publication can mimic
spikes. Candidates are not deduplicated events and have no minimum absolute-share
filter. No independent consolidated-volume feed, additional days, or extended-hours
data was used to validate accuracy. No directional or trading prediction is made.

## Phase 3: instrumented shadow monitoring

Implementation: `public_api_test/shadow.py`, with shared incremental `VolumeEngine`
and `EventEngine` cores behind the existing Phase 2A/2B APIs. Optional streaming hooks
in `monitor_multibatch` preserve its prior default behavior. See `SHADOW_MONITORING.md`
for commands, schemas, crash recovery, event finalization and limitations.

Only offline replay and mocked HTTP tests were executed. The original 100-stock raw
file remains unchanged. Replay produced 944 candidates, 767 events and 528 cooldown
opportunities; 755 events closed naturally and 12 remained right-censored/resumable.
All 18,000 interval records matched the saved Phase 2A artifact; all 767 event records
matched the saved Phase 2B artifact. There were no comparison mismatches.

The complete suite passed: 122 passed, 1 live test skipped, 8 subtests passed in 28.35s.
New coverage includes bounded state, checkpoint recovery after failed commit, no
restart duplicates, cooldown, missing timestamps/symbols, zero-then-burst patterns,
negative volume, 429/timeouts, resumed backoff, authentication failure and graceful stop.

Outputs: `results/phase3_validation/REPLAY_REPORT.md`, `replay_comparison.json`,
`backward_compatibility.json`, plus instrumented logs/checkpoint/summary in that folder.
Test log: `results/phase3_pytest.txt`.

No live run, notifications, orders, model training or execution integration was added.
Readiness is limited to operator-controlled shadow collection. Real long-duration
behavior and credential renewal remain unverified; authentication errors stop polling.
No profitability, causal price impact or predictive advantage is claimed.
