"""Main entry point for Public.com API Tester."""

import sys
import argparse
from pathlib import Path

from public_api_test.config import API_KEY, DEFAULT_TEST_SYMBOLS
from public_api_test.tests import test_auth, test_quotes, test_freshness, test_rate_limits, test_batch, test_historical, test_streaming


def main():
    parser = argparse.ArgumentParser(
        description="Public.com API Real-Time Market Data Capability Tester"
    )
    parser.add_argument(
        "--phase",
        type=str,
        choices=["auth", "quotes", "freshness", "rate_limits", "batch", "historical", "streaming", "all"],
        default="all",
        help="Which test phase to run (default: all)",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="AAPL,NVDA,MSFT,TSLA,SPY",
        help="Comma-separated list of symbols to test",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in seconds for freshness test (default: 5)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Test duration in seconds for freshness test (default: 300)",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=100,
        help="Rate limit test budget",
    )
    
    args = parser.parse_args()
    
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    
    print("=" * 60)
    print("Public.com API Real-Time Market Data Capability Tester")
    print("=" * 60)
    print(f"API key loaded: {'Yes' if API_KEY and len(API_KEY) > 0 else 'No'}")
    print(f"Symbols: {','.join(symbols)}")
    print()
    
    if args.phase in ("auth", "all"):
        print(">>> Phase: Authentication")
        test_auth.main()
    
    if args.phase in ("quotes", "all"):
        print("\n>>> Phase: Quote Testing")
        test_quotes.main()
    
    if args.phase in ("freshness", "all"):
        print(f"\n>>> Phase: Freshness and Latency Test")
        print(f"  Interval: {args.interval}s, Duration: {args.duration}s")
        test_freshness.main()
    
    if args.phase in ("rate_limits", "all"):
        print("\n>>> Phase: Rate Limit Validation")
        test_rate_limits.main(symbols, args.budget)
    
    if args.phase in ("batch", "all"):
        print("\n>>> Phase: Batch Quote Capability")
        test_batch.main(symbols)
    
    if args.phase in ("historical", "all"):
        print("\n>>> Phase: Historical Market Data")
        test_historical.main(args.symbol if args.phase == "historical" else "AAPL")
    
    if args.phase in ("streaming", "all"):
        print("\n>>> Phase: Streaming / WebSocket")
        test_streaming.main()
    
    print("\n" + "=" * 60)
    print("All selected phases completed")
    print("=" * 60)


if __name__ == "__main__":
    main()