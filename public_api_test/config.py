# Public.com API Tester Configuration

import os
from pathlib import Path
from dotenv import load_dotenv

# Explicit paths make loading independent of the launch directory.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")
API_BASE_URL = "https://api.public.com"
API_KEY = os.getenv("PUBLIC_API_KEY") or os.getenv("PUBLIC_ACCESS_TOKEN")

# Test Symbols (Phase 3)
DEFAULT_TEST_SYMBOLS = ["AAPL", "NVDA", "MSFT", "TSLA", "SPY"]

# Market Hours (ET)
MARKET_OPEN_ET = "09:30"
MARKET_CLOSE_ET = "16:00"

# Polling Configuration
DEFAULT_POLL_INTERVAL_SECONDS = 5
MINIMUM_POLL_INTERVAL_SECONDS = 1

# Rate Limit Configuration
DEFAULT_RATE_LIMIT_BUDGET = 100  # requests per minute (configurable)
EXPONENTIAL_BACKOFF_BASE = 1.5
MAX_BACKOFF_SECONDS = 30

# Output Configuration
RESULTS_DIR = Path("results")
RAW_OBSERVATIONS_DIR = RESULTS_DIR / "raw"
SUMMARY_STATS_DIR = RESULTS_DIR / "summaries"

# Ensure directories exist
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RAW_OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_STATS_DIR.mkdir(parents=True, exist_ok=True)