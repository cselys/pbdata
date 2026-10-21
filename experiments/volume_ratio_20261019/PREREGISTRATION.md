# Public.com Volume-Ratio Experiment — Pre-registration

Run date (planned): 2026-10-19 (ET), after 16:00 ET regular-session close.
Project: `C:\Git\AI\datasrc\publiccom`
Target consumer: Real-Time Price Behavior Signals project
(`C:\Git\AI\Claude\realtimesignals`), NH/NL SIGNALS page `volume_ratio`.

This file is written **before any live Public.com market-data request** of this
experiment.

## Question

Can Public.com supply the data required by the signal project's *existing*
volume-ratio definition, and is that data internally consistent and aligned with
the Schwab-sourced minute bars the project already stores?

## The existing definition being tested (do not substitute)

For an event minute `t` (ET), with 1-minute **interval** volume `v`:

```
volume_ratio = sum(v[t-4 .. t]) / sum(v[t-14 .. t])       # 5-bar / 15-bar
```

* Numerator window = 5 completed minute bars ending at (and including) the event minute.
* Denominator window = 15 completed minute bars ending at (and including) the event minute.
* Windows may extend back before 09:30 ET (the relevant window starts 09:15 ET), so
  premarket 1-minute bars are in scope.
* Source in the project today: `data/scanner.sqlite3.minute_bars` (Schwab provider),
  consumed by `research/nh_nl_strategy_study/nh_nl_sidecar.py` and `backtest.py`.
* The project's `rules.compute_volume_ratio` is currently a reserved stub returning
  `UNVERIFIABLE_VOLUME`; this experiment does not change it.

## Planned Public.com requests (bounded, read-only)

Local pacing 20 requests/minute (>= 3 s spacing). Hard cap: 6 market-data requests.
No retries on 429/5xx/401/403/network errors — the run stops and records the failure.

1. POST `/userapigateway/marketdata/{accountId}/quotes` — `[SPY, QQQ, NVDA]`
2. GET `/userapigateway/historicdata/EQUITY/SPY/DAY/ONE_MINUTE?tradingSessionToggle=REGULAR_AND_EXTENDED_HOURS`
3. GET `.../EQUITY/QQQ/DAY/ONE_MINUTE?...`
4. GET `.../EQUITY/NVDA/DAY/ONE_MINUTE?...`
5. POST quotes `[SPY, QQQ, NVDA]` (post-bars snapshot for reconciliation timing)

Authentication uses the existing `PublicComClient` (`.env`), never printed.
Raw responses are stored sanitized; secrets are never serialized.

## Pre-registered decision rules

* `VOLUME_SEMANTICS = SUPPORTED` iff
  * consecutive 1-min bar volumes are **non-monotonic** (interval-like; cumulative
    would be monotonic non-decreasing and equal to a running total), **and**
  * the sum of bars from 04:00 ET through the last complete minute agrees with the
    contemporaneous quote cumulative `volume` within 2% for SPY (sanity reconciliation).
  Otherwise `REJECTED`; if evidence conflicts, `UNKNOWN`.
* `TIME_ALIGNMENT = PASS` iff bar `timestamp` strings carry an explicit ET offset,
  are 1-minute spaced, and the 09:29→09:30 boundary separates `preMarket` from
  `regularMarket`. Else `FAIL` / `UNKNOWN`.
* `REFERENCE_VOLUME = AVAILABLE` iff a full 15-consecutive 1-min window ending at a
  chosen early-session event minute (<= 09:44 ET) is retrievable from Public.com
  for at least two symbols. Else `UNAVAILABLE` / `UNKNOWN`.
* `RECONCILIATION = PASS` iff, for the overlapping regular-session minutes of today,
  Public.com per-minute interval volume and the project's Schwab `minute_bars`
  per-minute interval volume agree in relative terms (median absolute relative
  difference <= 25% AND Spearman rank correlation of the two minute series >= 0.8).
  `INCOMPLETE` if either series is unavailable; `FAIL` otherwise.
* `SIGNAL_RATIO = IMPLEMENTABLE` iff VOLUME_SEMANTICS==SUPPORTED AND
  REFERENCE_VOLUME==AVAILABLE AND RECONCILIATION==PASS. `NEEDS_MORE_EVIDENCE` if any
  component is UNKNOWN/INCOMPLETE. `NOT_IMPLEMENTABLE` if any component is
  REJECTED/FAIL/UNAVAILABLE.

A returned number alone is not evidence. Missing data is never treated as zero.

## Constraints

No orders, no account mutations, no production DB writes, no changes to the
Scanner, no commits. Schwab `minute_bars` is read strictly read-only.
