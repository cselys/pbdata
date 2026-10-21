#!/usr/bin/env python3
"""Public.com Volume Ratio CLI Tool.

Calculates the ratio of volume delta in the 10 seconds ending at a given time
(regular trading hours only) to volume delta in the 15 seconds starting at
that time (any session, including premarket).

Usage:
    python -m public_api_test.volume_cli --symbol AAPL --time "2026-10-16T14:07:43Z"
    python -m public_api_test.volume_cli --symbol AAPL --time "2026-10-16T14:07:43Z" --data-dir results/
    python -m public_api_test.volume_cli --symbol AAPL  (uses current time)

Output: volume ratio as a decimal with 3 decimal places (e.g., 0.452).
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_timestamp(value):
    """Parse an ISO timestamp string to a timezone-aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load_observations(data_dir):
    """Load all quote observations from JSONL files in the results directory.

    Searches recursively for observations.jsonl files under data_dir.
    """
    observations = []
    results_dir = Path(data_dir)

    for jsonl_file in results_dir.rglob("**/observations.jsonl"):
        try:
            with open(jsonl_file, "r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for obs in record.get("observations", []):
                        observations.append(obs)
        except (OSError, json.JSONDecodeError):
            continue

    return observations


def calculate_volume_ratio(observations, symbol, target_time):
    """Calculate volume ratio:

    Numerator: sum of volume_delta for observations in [t-10s, t]
               where market_session == "regular" (regular trading hours only)

    Denominator: sum of volume_delta for observations in [t, t+15s]
                 for any session (including premarket)

    Returns ratio rounded to 3 decimal places, or None if no data.
    """
    target_dt = parse_timestamp(target_time) if isinstance(target_time, str) else target_time
    if target_dt is None:
        return None

    # Filter and parse observations for this symbol
    parsed = []
    for obs in observations:
        if obs.get("symbol") != symbol:
            continue
        ts_str = obs.get("response_received_at_utc")
        ts = parse_timestamp(ts_str)
        if ts is None:
            continue
        vol_delta = obs.get("volume_delta")
        session = obs.get("market_session")
        parsed.append({
            "timestamp": ts,
            "volume_delta": vol_delta,
            "market_session": session,
        })

    if not parsed:
        return None

    # Sort by timestamp
    parsed.sort(key=lambda x: x["timestamp"])

    # Numerator: volume_delta in [t-10s, t] for regular session only
    num_start = target_dt - timedelta(seconds=10)
    numerator = 0.0
    for obs in parsed:
        if num_start <= obs["timestamp"] <= target_dt and obs["market_session"] == "regular":
            if obs["volume_delta"] is not None:
                numerator += obs["volume_delta"]

    # Denominator: volume_delta in [t, t+15s] for any session
    denom_end = target_dt + timedelta(seconds=15)
    denominator = 0.0
    for obs in parsed:
        if target_dt <= obs["timestamp"] <= denom_end:
            if obs["volume_delta"] is not None:
                denominator += obs["volume_delta"]

    if denominator == 0:
        return None

    ratio = numerator / denominator
    return round(ratio, 3)


def main():
    parser = argparse.ArgumentParser(
        description="Calculate volume ratio for a symbol at a given time."
    )
    parser.add_argument(
        "--symbol", required=True,
        help="Stock symbol (e.g., AAPL)"
    )
    parser.add_argument(
        "--time", default=None,
        help="Target time in ISO format (default: now)"
    )
    parser.add_argument(
        "--data-dir", default="results",
        help="Directory containing results/ subdirectories with observations.jsonl files"
    )

    args = parser.parse_args()

    target_time = parse_timestamp(args.time) if args.time else datetime.now(timezone.utc)

    observations = load_observations(args.data_dir)

    if not observations:
        print("No observations found.", file=sys.stderr)
        sys.exit(1)

    result = calculate_volume_ratio(observations, args.symbol, target_time)

    if result is None:
        print("No data available for the given symbol and time.", file=sys.stderr)
        sys.exit(1)

    print(f"{result:.3f}")


if __name__ == "__main__":
    main()