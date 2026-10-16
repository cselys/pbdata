"""Batch quote capability test for Public.com API."""

import argparse
import os
import sys
import json
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from public_api_test.config import DEFAULT_TEST_SYMBOLS
from public_api_test.client import PublicComClient


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_batch_quotes(symbols=None, batch_sizes=None, requests_per_minute=60):
    """CLI capability sweep, or exactly the explicitly selected batch sizes."""
    test_symbols = list(dict.fromkeys(symbols or DEFAULT_TEST_SYMBOLS))
    sizes = list(dict.fromkeys(batch_sizes if batch_sizes is not None else [1, 5, 10, 25, 50]))
    if not sizes or any(size <= 0 for size in sizes):
        raise ValueError("Batch sizes must be positive")
    client = PublicComClient(requests_per_minute=requests_per_minute)
    batch_results = {}
    try:
        for size in sizes:
            if size > len(test_symbols):
                print(f"Skipping batch size {size} (exceeds available symbols {len(test_symbols)})")
                continue
            subset = test_symbols[:size]
            try:
                result = client.get_quotes_batch(subset)
                if not isinstance(result, dict):
                    raise TypeError("Batch client must return structured diagnostics")
                result["latency_ms"] = result["http_latency_ms"]  # Legacy report field.
            except Exception as error:
                # Never serialize exception strings: they can contain credentials/URLs.
                result = dict(category="client_error", error=type(error).__name__, http_status=None,
                    latency_ms=None, returned_symbols=0, partial_failures=size,
                    completeness_ratio=0, symbols_requested=subset,
                    api_error_message="Local batch failure; no exception text logged.")
            batch_results[size] = result
            print(f"Batch size {size}: status={result.get('http_status')} category={result['category']} "
                  f"latency_ms={result.get('latency_ms')} returned={result['returned_symbols']}/{size}")
            print(json.dumps(result, indent=2))
        output_path = Path("results/batch_quote_results.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(batch_results, indent=2), encoding="utf-8")
        print(f"Batch results saved to: {output_path}")
        return batch_results
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Public.com Batch Quote Capability Test"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="AAPL,NVDA,MSFT,TSLA,SPY",
        help="Comma-separated list of symbols",
    )
    parser.add_argument(
        "--batch-sizes",
        type=str,
        default="1,5,10,25,50",
        help="Comma-separated batch sizes to test",
    )

    parser.add_argument("--requests-per-minute", type=int, default=60)
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    batch_sizes = [int(s.strip()) for s in args.batch_sizes.split(",") if s.strip()]

    results = test_batch_quotes(symbols, batch_sizes, args.requests_per_minute)
    sys.exit(0 if results and all(r["category"] == "success" for r in results.values()) else 1)


if __name__ == "__main__":
    main()