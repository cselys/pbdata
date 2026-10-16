# Stage 1 Offline Data Quality Audit Report

## Batch Stability Test Analyzed
- File: `results/raw/batch_stability_20261009T163813224545Z.jsonl`
- Summary: `results/summaries/batch_stability_20261009T163813224545Z.json`
- Configuration: 20 symbols per batch, 10-second polling interval, 1,800-second duration, 180 cycles.
- Clock skew tolerance: 0 seconds (as recorded in summary).

## A. COIN Cumulative-Volume Anomaly

### Findings
- One instance of cumulative volume decrease (reset) detected for symbol COIN.
- Occurred between cycle 54 and cycle 55.
- Cycle 54 COIN volume: 5,322,231
- Cycle 55 COIN volume: 5,319,956
- Difference: -2,275 (a decrease of 2,275 shares)
- Both observations were valid (HTTP 200, category: success).
- No session boundary occurred (entire test within regular trading hours, 12:38-13:08 ET).
- The decrease is small relative to the volume (~0.04% of the volume at cycle 54).

### Analysis
The cumulative volume reported by the provider decreased, which is impossible under normal circumstances unless the provider corrected a prior value, reset the cumulative counter, or there was a data anomaly. Given the small magnitude, it may represent a minor correction (e.g., fixing a previously reported volume that was too high) or a temporary glitch.

### Verification of Exclusion from Volume Spike Calculations
The batch stability implementation correctly handles cumulative volume resets:
- In `cycle_observations` (batch_stability.py), when `volume < old_volume`, the reason is set to `"cumulative_volume_reset"` and `volume_delta` is set to `None`.
- Thus, negative volume deltas are excluded from usable volume delta calculations, preventing them from being mistaken for legitimate trading activity in volume spike detection.

## B. Timestamp Classification

### Findings
The summary reports the following timestamp statistics (with clock skew tolerance = 0 seconds):
- Last future timestamps: 832
- Bid future timestamps: 1,680
- Ask future timestamps: 1,680
- Maximum negative-age magnitude: approximately -0.167 seconds
- Non-negative age statistics show normal progression (mean age ~3.1 seconds for last timestamp).

### Implementation Review
The `annotate_quote` function in `sessions.py` computes side ages and classifies timestamps as follows:
- `future_timestamp` if `side_age < 0`
- `missing_timestamp` if `side_age is None`
- `stale` if `side_age > max_age` or not in session
- `valid` otherwise

It also computes a `side_future_within_tolerance` flag:
- `True` if `-clock_skew_tolerance <= side_age < 0`
- `False` otherwise

With `clock_skew_tolerance = 0`, the condition `-0 <= side_age < 0` is impossible, so all future timestamps (`side_age < 0`) are classified as genuine anomalies and none are considered within tolerance.

### Verification
The counts of future timestamps in the summary align with the number of observations where `side_age < 0` for each side (last, bid, ask). The implementation correctly separates genuine future-timestamp anomalies from those within tolerance (though with zero tolerance, none are within tolerance).

### Clock-Skew Tolerance Configurability
The system already supports a configurable clock-skew tolerance:
- The `annotate_quote` function accepts a `clock_skew_tolerance` parameter.
- The `monitor_batches` function in `batch_stability.py` accepts a `tolerance` parameter (default 0) and passes it to `annotate_quote`.
- The CLI (`__main__.py`) exposes `--clock-skew-tolerance` for the `batch_stability` phase, which is passed to `run_batch_stability`.
- Therefore, users can adjust nursing home tolerance (e.g., to 0.5 seconds) to account for minor clock differences without treating them as anomalies.

## Conclusion
Stage 1 audit revealed one minor cumulative volume anomaly (COIN) and confirmed that timestamp classification and volume delta calculations are functioning as designed. The system already provides configurable clock-skew tolerance and properly excludes invalid negative volume deltas from volume spike calculations.

No changes to the core logic are required based on this audit. However, users may wish to adjust the clock-skew tolerance based on their environment (e.g., to 0.5 seconds) if they observe small future timestamps due to clock skew.

## Recommendations
- For production monitoring, consider setting a small clock-skew tolerance (e.g., 0.5 seconds) to avoid false positives from minor clock differences.
- The COIN anomaly should be monitored in future tests to see if it recurs; if frequent, it may warrant provider consultation.
- All existing validation logic (session boundaries, volume reset detection, timestamp classification) is working correctly.

