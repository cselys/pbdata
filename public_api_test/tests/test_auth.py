"""Authentication test for Public.com API."""

import os
import sys
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from public_api_test.config import API_KEY
from public_api_test.client import PublicComClient


from public_api_test.cli_entrypoint import cli_entrypoint


@cli_entrypoint
def test_authentication():
    """Test that the API key is valid and authentication works."""
    print("=" * 58)
    print("Phase 1: Authentication Test")
    print("=" * 58)

    if not API_KEY:
        print("FAIL: PUBLIC_API_KEY environment variable not set")
        print("   Set: export PUBLIC_API_KEY='your_api_key_here'")
        return False

    print(f"OK: API key loaded (length: {len(API_KEY)} chars)")

    try:
        client = PublicComClient()
        # Try to get a quote for AAPL
        result = client.get_quote("AAPL")

        if result is None:
            print("FAIL: Could not retrieve quote - endpoint may be unavailable or unauthorized")
            print("   HTTP status: None (request failed)")
            client.close()
            return False

        if result.get("http_status") == 401:
            print("FAIL: Authentication failed - 401 Unauthorized")
            print("   The provided API key is invalid or expired")
            client.close()
            return False
        elif result.get("category") == "success":
            print("OK: Authentication successful - HTTP 200")
            print(f"   Symbol: {result.get('symbol')}")
            print(f"   Price: {result.get('price')}")
            print(f"   Provider timestamp: {result.get('provider_timestamp')}")
            client.close()
            return True
        elif result.get("http_status") == 403:
            print("FAIL: Authentication forbidden - 403 Forbidden")
            print("   The API key may not have market data permissions")
            client.close()
            return False
        else:
            print(f"? Partial - HTTP {result.get('http_status')}")
            print(f"   Response: {json.dumps(result.get('raw_response', {}), indent=2)[:500]}")
            client.close()
            return False

    except Exception as e:
        print(f"FAIL: Exception during authentication test: {e}")
        return False


if __name__ == "__main__":
    success = test_authentication()
    sys.exit(0 if success else 1)
def main():
    """Entry point for standalone execution."""
    test_authentication()


if __name__ == "__main__":
    main()
