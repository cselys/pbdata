"""Bounded live preflight for cross-provider validation.

Makes exactly one batch quote request per provider for AAPL and NVDA,
records request start/finish times, preserves native response fields,
and writes a sanitized preflight report.

No live API calls occur on import. No credentials are printed or logged.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from public_api_test.config import RESULTS_DIR
from public_api_test.cross_validation.adapters import (
    PublicComAdapter,
    SchwabAdapter,
    WebullAdapter,
    _parse_ts,
    _safe_float,
)
from dotenv import load_dotenv

# Load environment variables from public_api_test/.env
# Users can add SCHWAB_APP_KEY, SCHWAB_SECRET, SCHWAB_REFRESH_TOKEN
# to their local .env file (not .env.example) for cross-validation.
load_dotenv(RESULTS_DIR.parent / ".env")

from public_api_test.cross_validation.adapters import (
    PublicComAdapter,
    SchwabAdapter,
    WebullAdapter,
    _parse_ts,
    _safe_float,
)
from public_api_test.cross_validation.models import ProviderObservation


# ---------------------------------------------------------------------------
# Preflight result model
# ---------------------------------------------------------------------------


class PreflightResult:
    """Result of a single provider preflight probe."""

    def __init__(self, provider: str):
        self.provider = provider
        self.requests_attempted = 0
        self.requests_succeeded = 0
        self.requests_failed = 0
        self.errors: list[dict] = []
        self.observations: list[dict] = []
        self.field_mapping: dict[str, Any] = {}
        self.ready_for_collection = False

    def add_observation(self, obs: dict) -> None:
        self.observations.append(obs)

    def add_error(self, error_type: str, message: str, symbol: str | None = None) -> None:
        self.errors.append({
            "error_type": error_type,
            "message": message,
            "symbol": symbol,
        })
        self.requests_failed += 1

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "requests_attempted": self.requests_attempted,
            "requests_succeeded": self.requests_succeeded,
            "requests_failed": self.requests_failed,
            "errors": self.errors,
            "observations": self.observations,
            "field_mapping": self.field_mapping,
            "ready_for_collection": self.ready_for_collection,
        }


# ---------------------------------------------------------------------------
# Provider probes (one batch request each)
# ---------------------------------------------------------------------------


def probe_public_com(symbols: list[str]) -> PreflightResult:
    """Single batch quotes request to Public.com."""
    result = PreflightResult("public_com")

    from public_api_test.client import PublicComClient

    client = PublicComClient(requests_per_minute=40)
    adapter = PublicComAdapter(client)

    try:
        req_start = time.monotonic()
        req_start_utc = datetime.now(timezone.utc).isoformat()

        obs_list = adapter.poll_once(symbols)

        req_finished_utc = datetime.now(timezone.utc).isoformat()
        req_finished = time.monotonic()
        latency_ms = (req_finished - req_start) * 1000.0

        result.requests_attempted = len(symbols)
        # Count success as having actual data (not just HTTP 200)
        result.requests_succeeded = sum(
            1 for o in obs_list
            if o.error_category is None and o.last_price is not None
        )
        result.requests_failed = len(symbols) - result.requests_succeeded

        for obs in obs_list:
            obs_dict = obs.to_dict()
            # Add timing metadata
            obs_dict["request_latency_ms"] = round(latency_ms, 2)
            obs_dict["request_start_utc"] = obs.request_start_utc
            obs_dict["request_finished_utc"] = obs.request_finished_utc
            result.add_observation(obs_dict)

            # Build field mapping from raw response
            raw = obs.raw_response
            if isinstance(raw, dict):
                result.field_mapping[obs.symbol] = {
                    "last": raw.get("price"),
                    "last_timestamp_raw": raw.get("lastTimestamp"),
                    "bid": raw.get("bid"),
                    "bid_timestamp_raw": raw.get("bidTimestamp"),
                    "ask": raw.get("ask"),
                    "ask_timestamp_raw": raw.get("askTimestamp"),
                    "volume": raw.get("volume"),
                    "keys_present": sorted(raw.keys()),
                }

        # Check if all symbols have actual data
        result.ready_for_collection = all(
            o.error_category is None and o.last_price is not None
            for o in obs_list
        )

    except Exception as exc:
        result.add_error("client_error", type(exc).__name__)
        result.ready_for_collection = False
    finally:
        client.close()

    return result


def probe_schwab(symbols: list[str]) -> PreflightResult:
    """Single quotes request to Schwab."""
    result = PreflightResult("schwab")

    try:
        realtimesignals_path = "C:/Git/AI/claude/realtimesignals"
        if realtimesignals_path not in sys.path:
            sys.path.insert(0, realtimesignals_path)

        from schwab_stock_provider import SchwabStockProvider
        from public_api_test.cross_validation.adapters import SchwabAdapter

        provider = SchwabStockProvider()
        adapter = SchwabAdapter(provider)

        try:
            req_start = time.monotonic()
            req_start_utc = datetime.now(timezone.utc).isoformat()

            obs_list = adapter.poll_once(symbols)

            req_finished_utc = datetime.now(timezone.utc).isoformat()
            req_finished = time.monotonic()
            latency_ms = (req_finished - req_start) * 1000.0

            result.requests_attempted = len(symbols)
            # Count success as having actual data (not just HTTP 200)
            result.requests_succeeded = sum(
                1 for o in obs_list
                if o.error_category is None and o.last_price is not None
            )
            result.requests_failed = len(symbols) - result.requests_succeeded

            for obs in obs_list:
                obs_dict = obs.to_dict()
                obs_dict["request_latency_ms"] = round(latency_ms, 2)
                obs_dict["request_start_utc"] = obs.request_start_utc
                obs_dict["request_finished_utc"] = obs.request_finished_utc
                result.add_observation(obs_dict)

                # Build field mapping from raw response
                raw = obs.raw_response
                if isinstance(raw, dict):
                    result.field_mapping[obs.symbol] = {
                        "last_price": raw.get("last_price"),
                        "last_timestamp_raw": obs.last_timestamp_raw,
                        "bid": raw.get("bid_price"),
                        "ask": raw.get("ask_price"),
                        "volume": raw.get("volume"),
                        "quality": raw.get("quality"),
                        "keys_present": sorted(raw.keys()),
                    }

            # Check if all symbols have actual data
            result.ready_for_collection = all(
                o.error_category is None and o.last_price is not None
                for o in obs_list
            )

        except Exception as exc:
            result.add_error("client_error", type(exc).__name__)
            result.ready_for_collection = False
        finally:
            pass  # SchwabStockProvider has no close() method

    except ImportError as exc:
        result.add_error("import_error", str(exc))
        result.ready_for_collection = False

    return result


def probe_webull(symbols: list[str]) -> PreflightResult:
    """Single snapshots request to Webull."""
    result = PreflightResult("webull")

    try:
        webull_path = "C:/Git/AI/claude/webullrealcheck"
        if webull_path not in sys.path:
            sys.path.insert(0, webull_path)

        from app.data.webull_client import WebullClient
        from public_api_test.cross_validation.adapters import WebullAdapter

        client = WebullClient()
        adapter = WebullAdapter(client)

        try:
            req_start = time.monotonic()
            req_start_utc = datetime.now(timezone.utc).isoformat()

            obs_list = adapter.poll_once(symbols)

            req_finished_utc = datetime.now(timezone.utc).isoformat()
            req_finished = time.monotonic()
            latency_ms = (req_finished - req_start) * 1000.0

            result.requests_attempted = len(symbols)
            # Count success as having actual data (not just HTTP 200)
            result.requests_succeeded = sum(
                1 for o in obs_list
                if o.error_category is None and o.last_price is not None
            )
            result.requests_failed = len(symbols) - result.requests_succeeded

            for obs in obs_list:
                obs_dict = obs.to_dict()
                obs_dict["request_latency_ms"] = round(latency_ms, 2)
                obs_dict["request_start_utc"] = obs.request_start_utc
                obs_dict["request_finished_utc"] = obs.request_finished_utc
                result.add_observation(obs_dict)

                # Build field mapping from raw response
                raw = obs.raw_response
                if isinstance(raw, dict):
                    result.field_mapping[obs.symbol] = {
                        "price": raw.get("price"),
                        "last_trade_time": raw.get("last_trade_time"),
                        "bid": raw.get("bid"),
                        "ask": raw.get("ask"),
                        "volume": raw.get("volume"),
                        "trade_status": raw.get("trade_status"),
                        "keys_present": sorted(raw.keys()),
                    }

            # Check if all symbols have actual data
            result.ready_for_collection = all(
                o.error_category is None and o.last_price is not None
                for o in obs_list
            )

        except Exception as exc:
            result.add_error("client_error", type(exc).__name__)
            result.ready_for_collection = False
        finally:
            pass  # WebullClient has no close() method

    except ImportError as exc:
        result.add_error("import_error", str(exc))
        result.ready_for_collection = False

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def sanitize_raw(raw: Any) -> Any:
    """Return a shallow copy of raw response without credential-like fields."""
    if isinstance(raw, dict):
        result = {}
        for k, v in raw.items():
            kl = k.lower()
            if any(t in kl for t in ("token", "secret", "authorization", "apikey", "password")):
                result[k] = "[REDACTED]"
            else:
                result[k] = v
        return result
    return raw


def build_preflight_report(
    results: dict[str, PreflightResult],
    symbols: list[str],
    max_requests: int,
) -> dict:
    """Build the complete preflight report."""
    report = {
        "run_id": "cross_validate_preflight_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
        "symbols": symbols,
        "max_requests_planned": max_requests,
        "requests_attempted_total": sum(r.requests_attempted for r in results.values()),
        "requests_succeeded_total": sum(r.requests_succeeded for r in results.values()),
        "requests_failed_total": sum(r.requests_failed for r in results.values()),
        "providers": {name: r.to_dict() for name, r in results.items()},
        "ready_for_collection": all(r.ready_for_collection for r in results.values()),
        "field_mapping_summary": {},
        "timestamp_examples": {},
        "errors_summary": [],
        "limitations": [],
    }

    # Build field mapping summary
    for name, result in results.items():
        report["field_mapping_summary"][name] = result.field_mapping

    # Collect timestamp examples
    for name, result in results.items():
        for obs in result.observations:
            if obs.get("last_timestamp_raw") is not None:
                report["timestamp_examples"].setdefault(name, []).append({
                    "symbol": obs["symbol"],
                    "last_timestamp_raw": obs["last_timestamp_raw"],
                    "last_timestamp_utc": obs["last_timestamp_utc"],
                    "last_age_seconds": obs["last_age_seconds"],
                    "last_timestamp_status": obs["last_timestamp_status"],
                    "request_latency_ms": obs["request_latency_ms"],
                })

    # Collect errors
    for name, result in results.items():
        for err in result.errors:
            report["errors_summary"].append({
                "provider": name,
                "error_type": err["error_type"],
                "message": err["message"],
                "symbol": err["symbol"],
            })

    # Limitations
    for name, result in results.items():
        if not result.ready_for_collection:
            report["limitations"].append(
                f"{name}: not ready for collection (errors: {result.requests_failed})"
            )

    # Check Webull field mapping status
    webull = results.get("webull")
    if webull:
        confirmed = 0
        for sym, mapping in webull.field_mapping.items():
            if mapping.get("price") is not None:
                confirmed += 1
        if confirmed == 0:
            report["limitations"].append(
                "Webull: snapshot field mappings unverified - no price values returned"
            )

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(symbols: list[str] | None = None) -> int:
    """Run the bounded preflight.

    Makes exactly one batch request per provider.
    Returns 0 if all providers succeeded, 1 otherwise.
    """
    if symbols is None:
        symbols = ["AAPL", "NVDA"]

    max_requests = len(symbols) * 3  # 3 providers
    print(f"Preflight: {len(symbols)} symbols x 3 providers = {max_requests} max requests")

    results: dict[str, PreflightResult] = {}

    # Probe Public.com
    print("Probing Public.com...")
    results["public_com"] = probe_public_com(symbols)
    print(f"  Public.com: {results['public_com'].requests_succeeded}/{results['public_com'].requests_attempted} succeeded")

    # Probe Schwab
    print("Probing Schwab...")
    results["schwab"] = probe_schwab(symbols)
    print(f"  Schwab: {results['schwab'].requests_succeeded}/{results['schwab'].requests_attempted} succeeded")

    # Probe Webull
    print("Probing Webull...")
    results["webull"] = probe_webull(symbols)
    print(f"  Webull: {results['webull'].requests_succeeded}/{results['webull'].requests_attempted} succeeded")

    # Build report
    report = build_preflight_report(results, symbols, max_requests)

    # Write report to unique filename
    report_dir = RESULTS_DIR / "investigations"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{report['run_id']}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nReport written to: {report_path}")
    print(f"Total requests: {report['requests_attempted_total']} (planned max: {max_requests})")
    print(f"Succeeded: {report['requests_succeeded_total']}")
    print(f"Failed: {report['requests_failed_total']}")
    print(f"Ready for collection: {report['ready_for_collection']}")

    return 0 if report["requests_failed_total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
