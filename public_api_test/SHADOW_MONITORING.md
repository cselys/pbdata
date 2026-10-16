# Phase 3: instrumented shadow monitoring

The scanner records volume anomalies only. It has no notification, order, or execution path.
Use `python -m public_api_test.shadow --mode replay` for offline validation, or explicitly
select `--mode live` to contact Public.com. Existing tester commands are unchanged.

## Commands

Run from `C:\Git\AI\datasrc\publiccom`.

Offline replay into a **new** directory:

```powershell
python -m public_api_test.shadow --mode replay `
  --input results\raw\multi_batch_stability_20261012T141239357397Z.jsonl `
  --output-dir results\phase3_replay_check
```

Resume an interrupted replay, or verify that a completed replay is idempotent:

```powershell
python -m public_api_test.shadow --mode replay `
  --input results\raw\multi_batch_stability_20261012T141239357397Z.jsonl `
  --output-dir results\phase3_replay_check --resume
```

Live shadow monitoring (provided for the operator; **not run during implementation**):

```powershell
python -m public_api_test.shadow --mode live `
  --symbols-file public_api_test\config\scanner_symbols_100.txt `
  --interval 10 --duration 1800 --requests-per-minute 40 `
  --threshold 3.0 --window 30 --min-samples 10 `
  --event-gap 30 --cooldown 120 --max-age 60 --max-gap 30 `
  --output-dir results\shadow_session_001
```

Resume that session with exactly the same command plus `--resume`. Duration is the
polling budget for the new invocation (authentication is outside that budget). A
started sweep normally completes even if its duration crosses the deadline. Ctrl+C
or SIGTERM completes/commits the in-flight batch, then stops before the next batch.
The existing HTTP timeout still applies. A session directory permits only one writer.
A fresh run refuses to overwrite an existing directory. `--symbols AAPL,NVDA` can
replace `--symbols-file`; the default is the existing 100-stock file.

```powershell
python -m pytest public_api_test/tests/ -v
```

## Shared computation and timing

`VolumeEngine` is now the incremental core of the unchanged Phase 2A `detect` API.
`EventEngine` is the incremental core of Phase 2B `cluster`. Offline adapters retain
all original output fields, including offline candidate-line lists. Live state stores
only the previous volume sample, a bounded baseline deque, one active event, cooldown,
quality state and aggregate counters per symbol. No candidate history grows in memory.
The optional streaming hooks on `monitor_multibatch` retain its existing defaults.
Live mode uses `retain_sweeps=False` and processes each batch immediately after receipt.

The baseline is sum(prior valid volume deltas) / sum(prior elapsed receive-time seconds).
Current intervals enter it only after evaluation. Missing/invalid/stale/future samples,
negative corrections, session/date boundaries and excessive gaps retain Phase 2A rules.
Zero deltas are valid. Missing deltas are never imputed. A volume-specific timestamp
is never manufactured; Last timestamp is only a freshness proxy.

Event start/decision time is the first candidate's observation receipt, as in Phase 2B.
`decision_available_at_utc` and transition `processed_at_utc` record batch-processing
entry time, so availability/processing delay is auditable; these are not exchange times
or precise CPU-completion timestamps. End, peak and aggregate volume evolve as new
samples arrive. A new cluster inside cooldown is recorded with `cooldown_suppressed=true`;
no alert is sent even when `alert_eligible=true`. Total event volume includes valid
non-candidate intervals between candidates; trailing quiet intervals are not included.

Scheduling and measured request/operation durations use monotonic clocks. Provider age
and per-symbol elapsed time use recorded UTC receipt times for compatibility/auditing;
wall-clock corrections can therefore invalidate an interval. The 10-second interval
applies to sweep starts, not individual requests. At 100 symbols, each sweep contains
five sequential Quotes POSTs of at most 20 instruments. Existing global client spacing
and Retry-After handling apply to authentication and quote calls. The 40 RPM setting
is a configured local budget, not a claim about an API entitlement. Slow sweeps skip
elapsed schedule slots instead of launching catch-up bursts.

## Output schemas (version 1)

All JSONL files contain one JSON object per line. UTC times are timezone-aware ISO 8601;
missing values are JSON null. JSON and CSV use UTF-8. Credentials/account fields are
sanitized by the existing client and a second persistence boundary. No credentials are
included in checkpoints. Local files still contain market observations and symbol lists.

| File | Record and fields |
|---|---|
| `observations.jsonl` | One committed input transaction: `shadow_transaction` integer, `processed_at_utc`, `observations[]`, `batches[]`, sweep timing. Live transactions contain one batch and `expected_symbols[]`; offline source sweeps may contain all batches. Original observation fields (Last/Bid/Ask raw timestamps, price, raw cumulative volume and raw quote payload) are retained with sensitive fields redacted. |
| `observations.jsonl` batch | `batch_index`, requested/returned/missing symbols, `http_status`, `category`, sanitized API errors inside `batch`, `request_start_utc`, nullable `response_received_at_utc`, `http_latency_ms` (client monotonic HTTP timer), `operation_duration_monotonic_ms` (collector wrapper including parsing), `batch_start_offset_seconds`. Network failure has no actual HTTP receive timestamp; the observation's fallback time is explicitly labeled by the collector. |
| `observations.jsonl` scheduling | `scheduling_lateness_seconds`, nullable `actual_polling_interval_seconds`, `batch_skew_seconds`, `final_batch_in_sweep`. Polling interval/jitter summary samples use the first batch; skew uses completed final batches. Per-batch offsets preserve partially interrupted sweeps. |
| `intervals.jsonl` | One symbol observation: `transaction`, Phase 2A `input_line`, symbol/session, current/previous receipt and volume, elapsed seconds, delta, shares/sec rate, baseline method/rate/count, RVOL and exclusion/status. Additional `raw_cumulative_volume`, `volume_observation_quality` (`missing`, `invalid`, `negative`, `nonnegative`), `rejected_negative_volume_change`, `actual_observation_interval_seconds`, `prior_zero_intervals`, `zero_then_burst_suspected`. |
| `events.jsonl` | One transition: `transition_id` = run:transaction:ordinal, `transaction`, `transition` = open/update/close, observation `at`, processing `processed_at_utc`, `reason`, and full `event` snapshot. |
| Event snapshot | `event_id` = persistent-run-id:symbol:opening-input-line; symbol, decision time/availability/input line, start/end, volume interval start, session, peak/decision RVOL, decision baseline method/rate/count, total/candidate volume, candidate count, duration/volume span, `alert_eligible`, `cooldown_suppressed`. End/peak describe only observations received so far. No unbounded candidate-lines array is stored live. |
| `events.csv` | Append-only flattened transition log: transition ID/transaction/type/time/reason and event ID/symbol/decision times/start/end/peak RVOL/volume/count/duration/cooldown flags. It is **not** one row per final event; fold snapshots by event ID to obtain latest state. |
| `checkpoint.json` | Atomic internal recovery state: schema version, run ID, exact config/source identity, last transaction, bounded detector/event/diagnostic state, aggregate metrics/counters, runtime backoff deadline, and committed byte offsets for all four logs. Do not edit manually. |
| `summary.json` | Atomic derived snapshot: status/error, committed transactions, config/source, interval counts, transition/request status/category counts, timing aggregates (count/total/min/max/mean), per-symbol counters and polling intervals, active right-censored event snapshots, and last invocation timing/skipped slots. Checkpoint is authoritative if summary generation was interrupted. |
| `failure.json` | Written on live persistence/processing failure after an uncommitted mutation: safe exception class and `requires_resume=true`. Stop and resume; never continue with the poisoned in-memory engine. This file is historical diagnostic evidence if retained after recovery. |
| Replay artifacts | `replay_comparison.json` and `REPLAY_REPORT.md`: source hash/config, offline/live candidate-event-opportunity comparison, every-interval and event-field mismatch counts, natural closures and right-censored events. |

Intervals with absent rows are counted as missing and close the affected symbol's event;
no fake observation is inserted. Live collector failures normally produce explicit rows
for every requested symbol. Partial-batch records must be replayed using the shadow CLI:
it honors `expected_symbols` without treating the other batches as missing. Passing
individual batch lines directly to the legacy Phase 2A whole-sweep CLI would reset other
symbols; use the shadow replay adapter instead.

## Persistence and failure behavior

Each batch transaction appends and fsyncs observations, intervals and JSONL/CSV event
transitions, then atomically replaces the checkpoint with new output offsets and state.
On resume, bytes beyond committed offsets are truncated; checkpoint state and IDs are
restored together. A checkpoint/config/source mismatch or missing committed data fails
closed. Replay resumes only with the same input SHA-256 and skips committed input lines.
Live restart preserves baselines/cooldown; actual elapsed-time, session and quality rules
invalidate discontinuous samples. Monotonic epochs are not persisted. Retry-After is
saved as a UTC not-before deadline and honored before resumed authentication, together
with at least one local spacing interval. Clock reliability matters for this conversion.

Temporary quote failures/429/timeouts become diagnostic rows; subsequent polling proceeds
under the limiter. Authentication/authorization errors (401/403) stop polling and report
`authentication_failed`. Credentials must be corrected/refreshed externally before resume.
There is no automatic token renewal. Setup failure, all-invalid runs, partial/request
failures and persistence failures have explicit summary states. Degraded runs return a
nonzero exit status. A normal shutdown retains active events as resumable/right-censored;
it does not invent a close or delete cooldown state.

Access-token lifetime is configurable: `--validity-in-minutes` (default 450) is sent as
`validityInMinutes` to the documented token-exchange endpoint
(`POST /userapiauthservice/personal/access-tokens`). Public.com's quickstart shows the
parameter (example value 123) but documents no minimum or maximum, so acceptance of the
default must be verified by the live authentication check before a full session; if the
exchange rejects it, setup fails explicitly and nothing polls. The default of 450 minutes
targets a 6.5-hour RTH session plus buffer. `--credential-source` selects how the client
obtains its token: `fresh` (shadow live default) always exchanges `PUBLIC_API_KEY` for a
newly issued token and never reuses `PUBLIC_ACCESS_TOKEN`; `auto` preserves the previous
behavior of reusing `PUBLIC_ACCESS_TOKEN` when it is set.

Opt-in timing diagnostics: `--diagnostics-timing` adds stage-level timing fields to
committed batch records and the checkpoint runtime when enabled. The fields are
`idle_wait_monotonic_ms` (scheduled-slot and rate-limiter wait), `postprocess_duration_
monotonic_ms` (observation processing after the operation timer stops, before persistence),
`wall_monotonic_drift_ms` (signed wall-clock minus monotonic span over the batch window;
detects clock steps), `commit_duration_monotonic_ms` (whole persistence call), and the
commit substages `append_fsync_ms`, `checkpoint_write_ms`, `summary_write_ms` and
`other_commit_ms`. Per-batch commit timings are snapshotted into the checkpoint's
`runtime.last_commit_timing` (latest transaction only); `operation_duration_monotonic_ms`
keeps its existing meaning. Historical sessions without timing fields remain reportable.

Live event closure occurs on the next observation that establishes a quiet gap, quality
break or session change. Its `at` is the evidence receipt time; event `end` remains the
last candidate receipt. This is intentionally later than the offline end descriptor.
An entirely missing row closes immediately online; offline Phase 2B's row-only adapter
may not finalize it until the next row. No observations means no inferred quiet-time
close. Phase 3 does not interpolate feed silence into evidence of no activity.

## Validation and limitations

The recorded 100-stock dataset replay matched 944 candidates, 767 events and 528
cooldown-filtered opportunities, with zero interval or event-field mismatches. There
were 755 naturally closed events and 12 right-censored active events. The original
SHA-256 remains `8ee8d2760d3fdf21d6ff9fa600d5670570eeadcd1ee5f97132846af51f6cb6bc`.
All 18,000 interval records also match the previously saved Phase 2A output exactly,
and all 767 event records match the previously saved Phase 2B output exactly.
The complete offline suite passed: **122 passed, 1 live test skipped, 8 subtests passed
in 28.35 seconds**. HTTP/socket I/O is blocked by the existing pytest guard.
See `results/phase3_validation/REPLAY_REPORT.md`,
`results/phase3_validation/backward_compatibility.json` and `results/phase3_pytest.txt`.

- No live run was performed. Real credential renewal, network outages and long-duration
  OS/process behavior remain unverified against Public.com.
- Existing client tokens may expire; authentication failures stop the scanner. A new
  process reuses the existing authentication/account-selection flow, not a saved token.
- Last trade timestamps do not establish volume freshness. Zero-then-burst flags are
  suspicions, not confirmed reporting delays or confirmed economically meaningful events.
- Exactly-once behavior covers committed local logs and replay input lines, not HTTP
  requests. A crash after an HTTP response but before checkpoint commitment may lose that
  sample. There is no server cursor/backfill. On restart the scanner polls current data.
- Atomic replacement/fsync and a single-writer lock protect local process-crash recovery;
  this is not replicated storage or a guarantee against disk corruption/power loss.
  Do not move individual output files, edit offsets, or use an unsupported network filesystem.
- Live memory is bounded by configured universe/window and one batch. Logs grow on disk;
  rotation/retention/disk-space management are operator responsibilities. The offline
  replay comparison deliberately materializes the reference dataset for auditing.
- Data-quality thresholds and event-gap/cooldown values are configurable research choices,
  not validated alert settings. Live timing and batching can change boundaries relative
  to this one historical recording.
- This is shadow data collection, with no demonstrated predictive advantage, profitability,
  user-facing alerts or trading execution. Additional days, sessions and independent
  volume evidence are needed before considering alert quality.

## Session-end market coverage

On terminal summary states (including completed, interrupted and replay_complete),
the scanner now writes `session_end.json` and `SESSION_END_REPORT.md`; `summary.json`
includes the same `market_coverage` object. Existing runtime `status` semantics are
unchanged: `completed` describes process completion, not complete market coverage.
Coverage reports use only checkpoint-committed observations and intervals. They
identify partial recorded sweeps, absent/unserved symbols, unsuccessful quotes,
per-symbol receipt gaps over `max_gap`, and detector quality exclusions. Warmup
and initial observations are reported but are not themselves collection failures.

Coverage scope is explicitly the configured universe within recorded sweeps. Entire
unrecorded sweeps, unlogged invocations, and full scheduled trading-day coverage are
not inferred. Sweep-counter decreases identify segment boundaries; without invocation
IDs, a restart at the same sweep number cannot always be distinguished. The report
includes observed market-session labels and receive-time bounds, not a claim of full
pre-market/regular/after-hours coverage. It scans logs at session end, not on each batch.

Generate the same report for an existing session without modifying it:

```powershell
python -m public_api_test.shadow_report --session results\shadow_session_002 `
  --output-dir results\audits\shadow_session_002\coverage
```

The standalone report refuses output inside the session and never runs recovery,
truncation, authentication or API requests. Uncommitted tails are ignored using the
checkpoint's byte offsets. Its JSON contains `status`, `reasons`, observed segment/sweep
counts, expected/actual/missing symbol samples, partial-sweep symbol lists, per-symbol
sampling/quote counts, quality exclusions, observation bounds and session labels.

The targeted cooldown regression now restores a valid rolling baseline while the
previous 120-second cooldown is still binding. A new event at +30 seconds is suppressed;
a negative control with deliberately cleared cooldown would be eligible. Suppression
does not extend cooldown, and another event is eligible exactly at +120 seconds.
