"""Main entry point for Public.com API Tester."""

import sys
import argparse
from pathlib import Path

from public_api_test.config import API_KEY, DEFAULT_TEST_SYMBOLS
from public_api_test.tests import (
    test_auth, test_quotes, test_freshness, test_rate_limits,
    test_batch, test_historical, test_streaming,
    test_session_bars, test_rth_quality, test_batch_stability
)


def main():
    parser = argparse.ArgumentParser(
        description="Public.com API Real-Time Market Data Capability Tester"
    )
    parser.add_argument(
        "--phase",
        type=str,
        choices=[
            "auth", "quotes", "freshness", "rate_limits", "batch",
            "historical", "streaming", "session_bars", "diagnostic",
            "rth_quality", "batch_stability", "multi_batch_stability", "quote_diagnostics", "cross_validate", "all"
        ],
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
        "--symbols-file",
        type=str,
        help="Path to a text file with one symbol per line (overrides --symbols)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in seconds for freshness/RTH/batch stability tests (default: 5)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Test duration in seconds for freshness/RTH/batch stability tests (default: 300)",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=100,
        help="Rate limit test budget",
    )
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=60,
        help="Local request rate cap (default: 60)",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=60.0,
        help="Data-age threshold in seconds for freshness validation (default: 60)",
    )
    parser.add_argument(
        "--clock-skew-tolerance",
        type=float,
        default=0.0,
        help="Clock-skew tolerance in seconds for future timestamps (default: 0)",
    )
    parser.add_argument(
        "--session",
        type=str,
        choices=["auto", "premarket", "regular", "afterhours"],
        default="auto",
        help="Session filter for freshness test (default: auto)",
    )
    parser.add_argument(
        "--bars-trading-session-toggle",
        type=str,
        choices=["REGULAR_HOURS", "REGULAR_AND_EXTENDED_HOURS", "ALL_SESSIONS"],
        default="REGULAR_AND_EXTENDED_HOURS",
        help="Trading session toggle for bars (default: REGULAR_AND_EXTENDED_HOURS)",
    )
    parser.add_argument(
        "--batch-sizes",
        type=str,
        help="Comma-separated batch sizes for batch quote test (e.g., 10,20,30)",
    )
    parser.add_argument(
        "--max-symbols-per-batch",
        type=int,
        default=20,
        help="Maximum symbols per batch in multi_batch_stability phase (default: 20)",
    )
    parser.add_argument(
        "--quote-symbols",
        type=str,
        help="Comma-separated list of symbols for quote_diagnostics phase",
    )
    parser.add_argument(
        "--cross-validate-symbols",
        type=str,
        help="Comma-separated list of symbols for cross_validate phase (overrides --symbols)",
    )
    parser.add_argument(
        "--matching-tolerance",
        type=float,
        default=1.0,
        help="Matching tolerance in seconds for cross-provider observation alignment (default: 1.0)",
    )
    
    args = parser.parse_args()
    
    # Load symbols from file if provided
    if args.symbols_file:
        with open(args.symbols_file, 'r') as f:
            symbols = [line.strip().upper() for line in f if line.strip()]
    elif args.phase == "cross_validate" and args.cross_validate_symbols:
        symbols = [s.strip().upper() for s in args.cross_validate_symbols.split(",") if s.strip()]
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    
    # Parse batch sizes if provided
    batch_sizes = None
    if args.batch_sizes:
        batch_sizes = [int(x.strip()) for x in args.batch_sizes.split(",") if x.strip()]
    
    print("=" * 60)
    print("Public.com API Real-Time Market Data Capability Tester")
    print("=" * 60)
    print(f"API key loaded: {'Yes' if API_KEY and len(API_KEY) > 0 else 'No'}")
    print(f"Symbols: {','.join(symbols)}")
    print()
    
    if args.phase == "multi_batch_stability":
        from public_api_test.multi_batch_stability import run_multi_batch_stability
        from public_api_test.multi_batch_stability import validate_symbols as validate_multi_symbols
        try:
            validate_multi_symbols(symbols)
        except ValueError as error:
            parser.error(str(error))
        sys.exit(0 if run_multi_batch_stability(
            symbols=symbols,
            interval_seconds=args.interval,
            duration_seconds=args.duration,
            requests_per_minute=args.requests_per_minute,
            max_age=args.max_age,
            clock_skew_tolerance=args.clock_skew_tolerance,
            max_symbols_per_batch=args.max_symbols_per_batch
        ) else 1)

    if args.phase == "quote_diagnostics":
        from public_api_test.quote_diagnostics import run_quote_diagnostics
        sys.exit(0 if run_quote_diagnostics(
            symbols=symbols,
            interval_seconds=args.interval,
            duration_seconds=args.duration,
            requests_per_minute=args.requests_per_minute,
            max_age=args.max_age,
            clock_skew_tolerance=args.clock_skew_tolerance,
            max_symbols_per_batch=args.max_symbols_per_batch
        ) else 1)

    if args.phase == "cross_validate":
        from public_api_test.cross_validation.collector import CrossValidationCollector
        import json

        public_adapter = None
        schwab_adapter = None
        webull_adapter = None

        # Public.com adapter (always available)
        from public_api_test.client import PublicComClient
        from public_api_test.cross_validation.adapters import PublicComAdapter
        public_client = PublicComClient(requests_per_minute=args.requests_per_minute)
        public_adapter = PublicComAdapter(public_client)

        # Schwab adapter (optional, requires realtimesignals project on path)
        try:
            _real_timesignals = "C:/Git/AI/claude/realtimesignals"
            if _real_timesignals not in sys.path:
                sys.path.insert(0, _real_timesignals)
            from schwab_stock_provider import SchwabStockProvider
            from public_api_test.cross_validation.adapters import SchwabAdapter
            schwab_provider = SchwabStockProvider()
            schwab_adapter = SchwabAdapter(schwab_provider)
        except Exception:
            schwab_adapter = None

        # Webull adapter (optional, requires webullrealcheck project on path)
        try:
            _webull = "C:/Git/AI/claude/webullrealcheck"
            if _webull not in sys.path:
                sys.path.insert(0, _webull)
            from app.data.webull_client import WebullClient
            from public_api_test.cross_validation.adapters import WebullAdapter
            webull_client = WebullClient()
            webull_adapter = WebullAdapter(webull_client)
        except Exception:
            webull_adapter = None

        collector = CrossValidationCollector(
            symbols=symbols,
            interval_seconds=args.interval,
            duration_seconds=args.duration,
            public_adapter=public_adapter,
            schwab_adapter=schwab_adapter,
            webull_adapter=webull_adapter,
        )
        summary = collector.run()
        print(json.dumps(summary, indent=2))
        sys.exit(0 if summary["total_observations"] > 0 else 1)

    if args.phase == "batch_stability":
        from public_api_test.batch_stability import run_batch_stability, validate_symbols as validate_batch_symbols
        try:
            validate_batch_symbols(symbols)
        except ValueError as error:
            parser.error(str(error))
        if args.batch_sizes is not None:
            parser.error("batch_stability sends one complete batch per cycle; --batch-sizes is not applicable")
        sys.exit(0 if run_batch_stability(
            symbols, args.interval, args.duration, args.requests_per_minute,
            max_age=args.max_age,
            clock_skew_tolerance=args.clock_skew_tolerance
        ) else 1)

    if args.phase == "rth_quality":
        from public_api_test.tests.test_rth_quality import test_rth_quality
        if not test_rth_quality(
            symbols=symbols,
            interval_seconds=args.interval,
            duration_seconds=args.duration,
            requests_per_minute=args.requests_per_minute,
            session=args.session,
            max_age=args.max_age,
            clock_skew_tolerance=args.clock_skew_tolerance,
            bars_trading_session_toggle=args.bars_trading_session_toggle,
        ):
            sys.exit(1)

    if args.phase == "session_bars":
        from public_api_test.tests.test_session_bars import test_session_bars
        sys.exit(0 if test_session_bars(
            symbols, args.session, args.bars_trading_session_toggle, args.requests_per_minute
        ) else 1)

    if args.phase == "diagnostic":
        import json
        from public_api_test.client import PublicComClient
        from public_api_test.sessions import annotate_quote
        client = PublicComClient(requests_per_minute=args.requests_per_minute)
        try:
            for record in client.prepare():
                print(json.dumps(record))
            if not client.token or not client.account_id:
                sys.exit(1)
            result = annotate_quote(
                client.get_quote(symbols[0]), 
                args.session, 
                args.max_age, 
                clock_skew_tolerance=args.clock_skew_tolerance
            )
            print(json.dumps(result, indent=2))
            sys.exit(0 if result["category"] == "success" and result["session_filter_match"] else 1)
        finally:
            client.close()

    if args.phase in ("auth", "all"):
        print(">>> Phase: Authentication")
        from public_api_test.tests.test_auth import test_authentication
        if not test_authentication():
            sys.exit(1)
    
    if args.phase in ("quotes", "all"):
        print("\n>>> Phase: Quote Testing")
        from public_api_test.tests.test_quotes import test_quotes
        if not test_quotes(symbols, args.session, args.max_age, args.requests_per_minute, args.clock_skew_tolerance):
            sys.exit(1)
    
    if args.phase in ("freshness", "all"):
        print(f"\n>>> Phase: Freshness and Latency Test")
        print(f"  Interval: {args.interval}s, Duration: {args.duration}s")
        from public_api_test.tests.test_freshness import test_freshness
        success = test_freshness(
            symbols=symbols,
            interval_seconds=args.interval,
            duration_seconds=args.duration,
            requests_per_minute=args.requests_per_minute,
            session=args.session, max_age=args.max_age, 
            clock_skew_tolerance=args.clock_skew_tolerance,
        )
        if not success:
            sys.exit(1)

    if args.phase in ("rate_limits", "all"):
        print("\n>>> Phase: Rate Limit Validation")
        from public_api_test.tests.test_rate_limits import test_rate_limits
        test_rate_limits(symbols=symbols, initial_budget=args.budget)
    
    if args.phase in ("batch", "all"):
        print("\n>>> Phase: Batch Quote Capability")
        from public_api_test.tests.test_batch import test_batch_quotes
        if not test_batch_quotes(symbols, batch_sizes, args.requests_per_minute):
            sys.exit(1)

if __name__ == "__main__":
    main()
