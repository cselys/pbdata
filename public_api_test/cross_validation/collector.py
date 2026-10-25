"""Cross-provider collection orchestrator.

Polls Public.com, Schwab, and Webull concurrently at a fixed interval,
records actual request start/finish times for each provider, and writes
timestamped JSONL + JSON summary files.

No live API calls occur on import.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from public_api_test.config import RAW_OBSERVATIONS_DIR, SUMMARY_STATS_DIR
from public_api_test.cross_validation.adapters import (
    PublicComAdapter,
    SchwabAdapter,
    WebullAdapter,
)
from public_api_test.cross_validation.models import ProviderObservation


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class CrossValidationCollector:
    """Polls multiple providers concurrently and records observations."""

    def __init__(
        self,
        symbols: list[str],
        interval_seconds: int = 10,
        duration_seconds: int = 900,
        public_adapter: Optional[PublicComAdapter] = None,
        schwab_adapter: Optional[SchwabAdapter] = None,
        webull_adapter: Optional[WebullAdapter] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if not self.symbols:
            raise ValueError("At least one symbol is required")
        if interval_seconds <= 0 or duration_seconds <= 0:
            raise ValueError("Interval and duration must be positive")

        if public_adapter is None and schwab_adapter is None and webull_adapter is None:
            raise ValueError("At least one provider adapter is required")

        self.interval_seconds = interval_seconds
        self.duration_seconds = duration_seconds
        self.public_adapter = public_adapter
        self.schwab_adapter = schwab_adapter
        self.webull_adapter = webull_adapter
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock

        self._observations: list[ProviderObservation] = []
        self._lock = threading.Lock()
        self._interrupted = False
        self._sweep_count = 0

    # ------------------------------------------------------------------
    def _poll_provider(self, adapter, provider_name: str, sweep: int,
                        scheduled_offset: float, actual_start_offset: float) -> None:
        """Poll one provider and store observations (thread-safe)."""
        try:
            obs_list = adapter.poll_once(self.symbols)
        except Exception as exc:  # pragma: no cover - defensive
            obs_list = [
                ProviderObservation(
                    provider=provider_name,
                    symbol=s,
                    sweep=sweep,
                    scheduled_offset_seconds=scheduled_offset,
                    actual_start_offset_seconds=actual_start_offset,
                    request_start_utc="",
                    request_finished_utc="",
                    http_status=None,
                    http_latency_ms=None,
                    category="adapter_error",
                    api_error_code=type(exc).__name__,
                    last_price=None,
                    last_timestamp_raw=None,
                    last_timestamp_utc=None,
                    last_age_seconds=None,
                    last_timestamp_status="missing_timestamp",
                    bid_price=None,
                    bid_timestamp_raw=None,
                    bid_timestamp_utc=None,
                    bid_age_seconds=None,
                    bid_timestamp_status=None,
                    ask_price=None,
                    ask_timestamp_raw=None,
                    ask_timestamp_utc=None,
                    ask_age_seconds=None,
                    ask_timestamp_status=None,
                    volume=None,
                    error_category="adapter_error",
                    raw_response=None,
                )
                for s in self.symbols
            ]

        with self._lock:
            for obs in obs_list:
                obs.sweep = sweep
                obs.scheduled_offset_seconds = scheduled_offset
                obs.actual_start_offset_seconds = actual_start_offset
            self._observations.extend(obs_list)

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """Run the collection loop until duration expires.

        Returns a summary dict with file paths and counts.
        """
        name = "cross_validate_" + self.wall_clock().strftime("%Y%m%dT%H%M%S%fZ")
        raw_path = RAW_OBSERVATIONS_DIR / (name + ".jsonl")
        summary_path = SUMMARY_STATS_DIR / (name + ".json")

        # Ensure output directories exist
        RAW_OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_STATS_DIR.mkdir(parents=True, exist_ok=True)

        start = self.clock()
        deadline = start + self.duration_seconds
        scheduled = start

        active_adapters = []
        if self.public_adapter is not None:
            active_adapters.append(("public_com", self.public_adapter))
        if self.schwab_adapter is not None:
            active_adapters.append(("schwab", self.schwab_adapter))
        if self.webull_adapter is not None:
            active_adapters.append(("webull", self.webull_adapter))

        if not active_adapters:
            raise ValueError("At least one provider adapter is required")

        try:
            while self.clock() < deadline:
                due = max(scheduled, self.clock())
                self.sleep(max(0, min(due, deadline) - self.clock()))
                if self.clock() >= deadline:
                    break

                sweep_start = self.clock()
                self._sweep_count += 1
                sweep = self._sweep_count
                scheduled_offset = scheduled - start
                actual_start_offset = sweep_start - start

                # Poll all active providers concurrently
                threads = []
                for provider_name, adapter in active_adapters:
                    t = threading.Thread(
                        target=self._poll_provider,
                        args=(adapter, provider_name, sweep,
                              scheduled_offset, actual_start_offset),
                        daemon=True,
                    )
                    threads.append(t)
                    t.start()

                for t in threads:
                    t.join()

                scheduled += self.interval_seconds

        except KeyboardInterrupt:
            self._interrupted = True

        # Write raw JSONL
        with raw_path.open("w", encoding="utf-8") as stream:
            for obs in self._observations:
                stream.write(obs.to_jsonl() + "\n")

        # Build summary
        summary = self._build_summary(name, raw_path, summary_path)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        return summary

    # ------------------------------------------------------------------
    def _build_summary(self, name: str, raw_path: Path, summary_path: Path) -> dict[str, Any]:
        per_provider: dict[str, dict[str, Any]] = {}
        for obs in self._observations:
            p = per_provider.setdefault(
                obs.provider,
                {
                    "observations": 0,
                    "symbols": set(),
                    "http_statuses": {},
                    "categories": {},
                    "missing_timestamps": 0,
                    "future_timestamps": 0,
                    "stale_timestamps": 0,
                    "invalid_timestamps": 0,
                    "valid_timestamps": 0,
                    "latencies": [],
                },
            )
            p["observations"] += 1
            p["symbols"].add(obs.symbol)
            if obs.http_status is not None:
                p["http_statuses"][str(obs.http_status)] = (
                    p["http_statuses"].get(str(obs.http_status), 0) + 1
                )
            p["categories"][obs.category] = p["categories"].get(obs.category, 0) + 1

            if obs.last_timestamp_status == "missing_timestamp":
                p["missing_timestamps"] += 1
            elif obs.last_timestamp_status == "future_timestamp":
                p["future_timestamps"] += 1
            elif obs.last_timestamp_status == "stale":
                p["stale_timestamps"] += 1
            elif obs.last_timestamp_status == "invalid_timestamp":
                p["invalid_timestamps"] += 1
            elif obs.last_timestamp_status == "valid":
                p["valid_timestamps"] += 1

            if obs.http_latency_ms is not None:
                p["latencies"].append(obs.http_latency_ms)

        # Convert sets to sorted lists for JSON serialization
        for p in per_provider.values():
            p["symbols"] = sorted(p["symbols"])
            if p["latencies"]:
                lat = sorted(p["latencies"])
                n = len(lat)
                p["http_latency_ms"] = {
                    "count": n,
                    "min": lat[0],
                    "max": lat[-1],
                    "mean": sum(lat) / n,
                    "median": lat[n // 2],
                }
            else:
                p["http_latency_ms"] = None
            del p["latencies"]

        total = len(self._observations)
        return {
            "run_id": name,
            "symbols": self.symbols,
            "providers": list(per_provider.keys()),
            "interval_seconds": self.interval_seconds,
            "duration_seconds": self.duration_seconds,
            "sweep_count": self._sweep_count,
            "total_observations": total,
            "interrupted": self._interrupted,
            "raw_path": str(raw_path),
            "summary_path": str(summary_path),
            "per_provider": per_provider,
        }
