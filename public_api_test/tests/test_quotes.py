"""Quote test for Public.com API."""

import os
import sys
import json
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from public_api_test.config import DEFAULT_TEST_SYMBOLS, API_KEY
from public_api_test.client import PublicComClient


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_quotes(symbols=None, session="auto", max_age=60, requests_per_minute=60, clock_skew_tolerance=0):
    """Test quote retrieval for sample symbols."""
    print("=" * 58)
    print("Phase 3: Quote Testing")
    print("=" * 58)

    symbols = symbols or DEFAULT_TEST_SYMBOLS
    print(f"Testing symbols: {','.join(symbols)}")

    client = PublicComClient(requests_per_minute=requests_per_minute)
    from public_api_test.sessions import annotate_quote
    results = []

    for symbol in symbols:
        print(f"\n--- Testing {symbol} ---")
        result = annotate_quote(client.get_quote(symbol), session, max_age, clock_skew_tolerance=clock_skew_tolerance)

        if result is None:
            print("  FAIL: No response received for {}".format(symbol))
            results.append({
                "symbol": symbol,
                "http_status": 0,
                "request_duration_ms": 0,
                "price": None,
                "bid": None,
                "ask": None,
                "provider_timestamp": None,
                "error": "no_response",
            })
            continue

        # Record results
        result_data = result.copy()
        print(json.dumps(result, indent=2))

        results.append(result_data)

        # Display results
        print(f"  HTTP Status: {result.get('http_status')}")
        print(f"  Request Duration: {result.get('request_duration_ms', 0):.1f} ms")

        price = result.get("price")
        if price is not None:
            print(f"  Last Price: {price}")
        else:
            print(f"  Last Price: None")

        bid = result.get("bid")
        if bid is not None:
            print(f"  Bid: {bid}")
        else:
            print(f"  Bid: None")

        ask = result.get("ask")
        if ask is not None:
            print(f"  Ask: {ask}")
        else:
            print(f"  Ask: None")

        pt = result.get("provider_timestamp")
        if pt is not None:
            print(f"  Provider Timestamp: {pt}")
        else:
            print(f"  Provider Timestamp: None (data age unverified)")

        di = result.get("delayed_indicator")
        if di is not None:
            print(f"  Delayed Indicator: {di}")

        # Capture response headers related to quotas
        if result.get("response_headers"):
            # Look for quota-related headers (don't log auth headers)
            quota_keys = [k for k in result["response_headers"].keys() if any(
                x in k.lower() for x in ['limit', 'quota', 'rate'])]
            if quota_keys:
                print(f"  Quota-Related Headers: {quota_keys}")

        client.close()

    # Summary
    print(f"\n{'=' * 58}")
    print("Quote Test Summary")
    print(f"{'=' * 58}")

    successful = sum(1 for r in results if r.get("category") == "success")
    print(f"Total symbols tested: {len(results)}")
    print(f"Successful valid quotes: {successful}")
    print(f"Failed: {len(results) - successful}")

    if successful > 0:
        prices = [r["price"] for r in results if r["price"] is not None]
        if prices:
            print(f"\nObserved prices: {prices}")
            print(f"Unique prices: {len(set(prices))}")

    # Save results
    output_path = Path("results/quote_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    client.close()
    return bool(results) and successful == len(results) and any(r["session_filter_match"] for r in results)


if __name__ == "__main__":
    success = test_quotes()
    sys.exit(0 if success else 1)
def main():
    """Entry point for standalone execution."""
    test_quotes()


if __name__ == "__main__":
    main()
