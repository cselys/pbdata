"""Historical market data test for Public.com API."""

import os
import sys
import json
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from public_api_test.client import PublicComClient


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_historical_data(symbol="AAPL", intervals=None):
    """
    Test historical OHLCV data availability.
    
    Check supported intervals, especially:
    - 1 minute
    - 5 minutes
    - 15 minutes
    - 1 day
    
    Evaluate:
    - Maximum lookback
    - Maximum candles per request
    - Timestamp consistency
    - Volume availability
    - Missing candles
    - Extended-hours inclusion
    """
    print("=" * 58)
    print("Phase 7: Historical Market Data")
    print("=" * 58)

    test_intervals = intervals or ["1m", "5m", "15m", "1d"]
    symbol = symbol.upper()

    print(f"Testing historical data for: {symbol}")
    print(f"Supported intervals to check: {test_intervals}")
    print()

    client = PublicComClient()

    historical_results = {}

    for interval in test_intervals:
        print(f"--- Interval: {interval} ---")

        try:
            # Request a small lookback
            start_time = time.time()
            result = client.get_historical(
                symbol=symbol,
                interval=interval,
                lookback=50,  # Reasonable small request
            )
            elapsed_ms = (time.time() - start_time) * 1000

            if result is not None and result.get("candles_returned", 0) > 0:
                candles = result.get("raw_response", {}).get("candles", []) if isinstance(result.get("raw_response"), dict) else []

                # Analyze candles
                has_volume = all("volume" in c for c in candles[:5]) if candles else False
                timestamp_consistency = result.get("timestamp_consistency", None)
                extended_hours = result.get("extended_hours_included", None)
                missing_candles = result.get("missing_candles", 0)

                print(f"  Candles returned: {result.get('candles_returned')}")
                print(f"  HTTP Latency: {elapsed_ms:.1f} ms")
                print(f"  Maximum available: {result.get('maximum_available')}")
                print(f"  Missing candles: {missing_candles}")
                print(f"  Has volume data: {has_volume}")
                if timestamp_consistency is not None:
                    print(f"  Timestamp consistency: {timestamp_consistency}")
                if extended_hours is not None:
                    print(f"  Extended-hours included: {extended_hours}")

                # Show sample candle structure
                if candles:
                    print(f"  Sample candle: {candles[0]}")

                historical_results[interval] = {
                    "candles_returned": result.get("candles_returned"),
                    "http_latency_ms": elapsed_ms,
                    "maximum_available": result.get("maximum_available"),
                    "missing_candles": missing_candles,
                    "has_volume": has_volume,
                    "timestamp_consistency": timestamp_consistency,
                    "extended_hours_included": extended_hours,
                    "success": True,
                }
            else:
                print(f"  No historical data available for {interval}")
                print(f"  HTTP Latency: {elapsed_ms:.1f} ms")

                historical_results[interval] = {
                    "candles_returned": 0,
                    "http_latency_ms": elapsed_ms,
                    "maximum_available": False,
                    "missing_candles": 0,
                    "has_volume": False,
                    "timestamp_consistency": None,
                    "extended_hours_included": None,
                    "success": False,
                    "error": "no_data_available",
                }

        except Exception as e:
            print(f"  Exception for {interval}: {e}")
            historical_results[interval] = {
                "candles_returned": 0,
                "http_latency_ms": 0,
                "error": str(e),
                "success": False,
            }

        # Delay between interval requests
        time.sleep(0.3)

    # Save results
    output_path = Path("results/historical_results.json")
    with open(output_path, "w") as f:
        json.dump(historical_results, f, indent=2, default=str)
    print(f"\nHistorical data results saved to: {output_path}")

    client.close()
    return historical_results


def main():
    parser = argparse.ArgumentParser(
        description="Public.com Historical Market Data Test"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="AAPL",
        help="Symbol to test historical data for",
    )
    parser.add_argument(
        "--intervals",
        type=str,
        default="1m,5m,15m,1d",
        help="Comma-separated intervals to test",
    )

    args = parser.parse_args()
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]

    test_historical_data(args.symbol, intervals)


if __name__ == "__main__":
    main()