"""Only a single quote, and only with explicit --run-live authorization."""
import pytest
from public_api_test.client import PublicComClient


@pytest.mark.live
def test_live_single_quote():
    client = PublicComClient()
    try:
        assert client.get_quote("AAPL")["category"] == "success"
    finally:
        client.close()
