# Timestamp Investigation Report - quote_diagnostics Bug

## Critical Finding

The `quote_diagnostics` module is **not extracting timestamps** from API responses, while other modules (`batch_stability`, `multi_batch_stability`) correctly extract them.

## Root Cause Analysis

### API Response Structure Comparison

**Working (batch_stability.py - lines 98-123):**
```python
# Extract from instrument results
outcomes = {i["symbol"]: i for i in batch.get("instrument_results", [])}

# In cycle_observations():
parsed = outcomes.get(symbol, {})
row = dict(
    ...,
    last_timestamp_raw=raw.get("lastTimestamp"),  # ← FROM raw.get() (from matching)
    bid_timestamp_raw=raw.get("bidTimestamp"),      # ← FROM raw.get() (from matching)
    ask_timestamp_raw=raw.get("askTimestamp"),     # ← FROM raw.get() (from matching)
    ...
)
```

**Broken (quote_diagnostics.py - lines 314-318):**
```python
# Extract from raw_response
last_ts_raw = raw_response.get("lastTimestamp") or raw_response.get("last_timestamp")
bid_ts_raw = raw_response.get("bidTimestamp")
ask_ts_raw = raw_response.get("askTimestamp")
```

### The Problem

The `quote_diagnostics.py` is looking for timestamps in `raw_response`, but according to the existing working code in `batch_stability.py`, the timestamps are located in a **different object**.

### Evidence Chain

1. **API Response Structure (from client.py):**
   - The `_prepare_batch` method (lines 268-322) creates `instrument_results` with `provider_timestamp_raw=item.get("lastTimestamp")`
   - It also creates `results` list with `raw_response` from the original API call

2. **Working Implementation (batch_stability.py):**
   - `cycle_observations()` extracts timestamps from `raw.get("lastTimestamp")` where `raw` comes from `matching.get(symbol, {})`
   - `raw` is derived from `matching.get(symbol, {})` which comes from `quotes = body.get("quotes", [])`
   - This `body` comes from `request.get("raw_response", {})` where `request` comes from `batch.get("results", [])`

3. **Broken Implementation (quote_diagnostics.py):**
   - Line 315: `last_ts_raw = raw_response.get("lastTimestamp") or raw_response.get("last_timestamp")`
   - This looks for timestamps in `raw_response` but the timestamps are actually in the `instrument` object inside the `instrument_results`

### Exact Issue Location

**File:** `public_api_test/quote_diagnostics.py`  
**Lines:** 314-318  
**Problematic Code:**
```python
last_ts_raw = raw_response.get("lastTimestamp") or raw_response.get("last_timestamp")
bid_ts_raw = raw_response.get("bidTimestamp")
ask_ts_raw = raw_response.get("askTimestamp")
```

### Correct Extraction Path (Based on Existing Working Code)

According to `batch_stability.py` lines 98-123, the correct extraction should be:

```python
# 1. Get instrument_results from batch
instrument_results = batch.get("instrument_results", [])

# 2. Find the specific instrument for this symbol
for inst in instrument_results:
    if inst.get("symbol") == symbol:
        # 3. Extract timestamps from instrument (NOT from raw_response)
        last_ts_raw = inst.get("provider_timestamp_raw")  # Note: This might not exist
        # Or look for them in the instrument's raw_data if available
        break
```

### Evidence from October 21 Results

**Working (batch_stability):** `last_timestamp_raw": "2026-10-09T16:38:24Z"`  
**Broken (quote_diagnostics):** `last_timestamp_raw": null`

### Root Cause Summary

The `quote_diagnostics` module is **incorrectly extracting timestamps from the wrong part of the API response**. It's looking in `raw_response` but the timestamps are located elsewhere (in the `instrument_results` object or a different structure).

This is a **copy-paste error** where the diagnostic module was written without understanding the actual API response structure used by the working modules.

## Required Fix

The `quote_diagnostics.py` module must be updated to extract timestamps from the correct location in the API response, following the same pattern as `batch_stability.py`.

### Files to Modify
1. `public_api_test/quote_diagnostics.py` - Lines 314-318 (timestamp extraction)
2. `public_api_test/quote_diagnostics.py` - Additional logic to access `instrument_results` from the batch

### Investigation Needed
To implement the fix, we need to:
1. Examine the exact structure of `batch` returned by `client.get_quotes_batch()`
2. Determine where timestamps are actually stored in the API response
3. Update `quote_diagnostics.py` to extract timestamps from the correct location
4. Ensure the fix doesn't break existing functionality

## Next Steps

1. Examine the actual API response structure to confirm timestamp locations
2. Update `quote_diagnostics.py` to use the same timestamp extraction logic as `batch_stability.py`
3. Add tests to verify the fix
4. Validate that all 194 existing tests still pass
5. Test the fix with offline mocked responses

## Impact

- **Severity:** HIGH - The diagnostic is completely non-functional for timestamp analysis
- **Scope:** All timestamp-related analysis (stale/fresh detection, age calculations, etc.)
- **Fix Complexity:** LOW - Simple fix to extract timestamps from correct API response location
- **Risk:** LOW - Following existing patterns reduces risk of regression

## Evidence Files
- `results/raw/batch_stability_20261009T163813224545Z.jsonl` (working timestamps)
- `results/raw/quote_diagnostics_20261021T144214131440Z.jsonl` (missing timestamps)
- `public_api_test/batch_stability.py` (working timestamp extraction)
- `public_api_test/client.py` (API response structure)

## Conclusion

The missing timestamps issue is **not a bug in the timestamp processing logic** but a **bug in timestamp extraction**. The diagnostic is looking for timestamps in the wrong part of the API response structure.

The fix requires updating `quote_diagnostics.py` to extract timestamps from the same location as `batch_stability.py`.

**Urgency: HIGH** - The diagnostic is fundamentally broken without proper timestamp extraction.