"""Bounded read-only Public.com 1-minute bar latency probe.

This probe is preparation only until explicitly approved. It:
  * uses the existing PublicComClient credential mechanism (no credentials read
    or printed here);
  * issues no orders and writes no volume cache;
  * samples a small fixed symbol set with a hard request cap;
  * records only request timings, status, bar counts, and latency;
  * never stores raw response bodies, headers, URLs with secrets, or messages.

Plan-only mode (default) performs no imports of the client and no network I/O:
    python -m experiments.volume_latency_probe_20261019.probe

Live mode requires the explicit approval flag:
    python -m experiments.volume_latency_probe_20261019.probe --execute
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

UTC = timezone.utc
RUN_ROOT = Path(__file__).resolve().parent
SYMBOLS = ("SPY", "QQQ", "NVDA")
ROUNDS = 2
MAX_SETUP_REQUESTS = 2
MAX_BARS_REQUESTS = len(SYMBOLS) * ROUNDS
MAX_TOTAL_REQUESTS = MAX_SETUP_REQUESTS + MAX_BARS_REQUESTS
REQUESTS_PER_MINUTE = 20
ROUND_GAP_SECONDS = 30.0
ENDPOINT_LABEL = "one_minute_bars"
PROBE_VERSION = "0.1.0"
_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def plan() -> dict:
    return {
        "probe_version": PROBE_VERSION,
        "symbols": list(SYMBOLS),
        "rounds": ROUNDS,
        "endpoint": ENDPOINT_LABEL,
        "requests_per_minute": REQUESTS_PER_MINUTE,
        "setup_request_cap": MAX_SETUP_REQUESTS,
        "bars_requests": MAX_BARS_REQUESTS,
        "hard_request_cap": MAX_TOTAL_REQUESTS,
        "round_gap_seconds": ROUND_GAP_SECONDS,
        "retries": 0,
        "stop_on": ["HTTP 429", "two consecutive request errors", "request cap"],
        "no_orders": True,
        "no_cache_writes": True,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _safe(value, default: str = "UNKNOWN") -> str:
    text = str(value) if value is not None else ""
    return text if _SAFE.fullmatch(text) else default


def _bar_summary(body) -> tuple[int, datetime | None]:
    if not isinstance(body, dict):
        return 0, None
    count = 0
    latest = None
    for key in ("preMarket", "regularMarket", "afterMarket"):
        group = body.get(key)
        if not isinstance(group, dict):
            continue
        for bar in group.get("bars") or []:
            if not isinstance(bar, dict):
                continue
            raw = bar.get("timestamp")
            if not isinstance(raw, str):
                continue
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(UTC)
            count += 1
            if latest is None or stamp > latest:
                latest = stamp
    return count, latest


def _latency_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min_ms": None, "max_ms": None, "mean_ms": None,
                "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    count = len(ordered)
    p50 = ordered[min(count - 1, int(round(0.50 * (count - 1))))]
    p95 = ordered[min(count - 1, int(round(0.95 * (count - 1))))]
    return {
        "count": count,
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / count, 3),
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def run_probe(client, output_dir: str | Path, *, symbols=SYMBOLS, rounds=ROUNDS,
              max_requests: int = MAX_TOTAL_REQUESTS,
              round_gap_seconds: float = ROUND_GAP_SECONDS,
              sleep=time.sleep, now=lambda: datetime.now(UTC),
              perf_counter=time.perf_counter) -> dict:
    """Run the bounded probe against an injected client. No retries."""
    run_id = now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    out = Path(output_dir)
    requests_path = out / "requests.jsonl"
    manifest_path = out / "manifest.json"
    started_at = now()
    requests: list[dict] = []
    setup: list[dict] = []
    status_counts: dict[str, int] = {}
    latencies: list[float] = []
    bars_requests = 0
    consecutive_errors = 0
    reason = "COMPLETED"
    outcome = "COMPLETED"
    exit_code = 0
    error_class = None

    def record_setup(records) -> None:
        for record in records or []:
            if not isinstance(record, dict):
                continue
            stage = _safe(record.get("stage"), "setup")
            entry = {
                "stage": stage,
                "http_status": record.get("http_status"),
                "category": _safe(record.get("category"), "unknown"),
                "request_duration_ms": record.get("request_duration_ms"),
            }
            setup.append(entry)
            status = entry["http_status"]
            if status is not None:
                status_counts[str(int(status))] = status_counts.get(str(int(status)), 0) + 1

    try:
        record_setup(client.prepare())
        setup_statuses = {entry["http_status"] for entry in setup}
        if len(setup) > MAX_SETUP_REQUESTS:
            reason = outcome = "SETUP_BUDGET_EXCEEDED"
            exit_code = 3
        elif 429 in setup_statuses:
            reason = outcome = "RATE_LIMITED"
            exit_code = 3
        elif setup_statuses & {401, 403} or not getattr(client, "token", None):
            reason = outcome = "AUTH_UNAVAILABLE"
            exit_code = 2
        else:
            stop = False
            for round_index in range(rounds):
                if stop:
                    break
                if round_index and round_gap_seconds > 0:
                    sleep(round_gap_seconds)
                for symbol in symbols:
                    if stop:
                        break
                    if len(setup) + len(requests) >= max_requests:
                        reason = outcome = "REQUEST_CAP"
                        exit_code = 3
                        stop = True
                        break
                    endpoint = (f"/userapigateway/historicdata/EQUITY/"
                                f"{quote(symbol, safe='')}/DAY/ONE_MINUTE")
                    request_started = now()
                    clock_start = perf_counter()
                    response = None
                    body = {}
                    client_duration_ms = None
                    call_error = None
                    try:
                        result = client._request(
                            "GET", endpoint,
                            params={"tradingSessionToggle": "REGULAR_AND_EXTENDED_HOURS"})
                        response, client_duration_ms, body, _headers = result
                    except Exception as exc:
                        call_error = type(exc).__name__
                    request_ended = now()
                    latency_ms = (perf_counter() - clock_start) * 1000.0
                    status = response.status_code if response is not None else None
                    if status is not None:
                        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
                    bar_count, latest_bar = _bar_summary(body) if status == 200 else (0, None)
                    if call_error is None and response is None and isinstance(body, dict):
                        call_error = _safe(body.get("error"), "NetworkError")
                    entry = {
                        "sequence": len(setup) + len(requests) + 1,
                        "kind": "bars",
                        "endpoint": ENDPOINT_LABEL,
                        "symbol": _safe(symbol, "UNKNOWN"),
                        "started_at_utc": _iso(request_started),
                        "ended_at_utc": _iso(request_ended),
                        "latency_ms": round(latency_ms, 3),
                        "client_duration_ms": client_duration_ms,
                        "http_status": status,
                        "error_class": call_error,
                        "bar_count": bar_count,
                        "max_bar_start_utc": _iso(latest_bar) if latest_bar else None,
                    }
                    requests.append(entry)
                    latencies.append(latency_ms)
                    bars_requests += 1
                    if status == 429:
                        reason = outcome = "RATE_LIMITED"
                        exit_code = 3
                        stop = True
                    elif status in (401, 403):
                        reason = outcome = "AUTH_UNAVAILABLE"
                        exit_code = 2
                        stop = True
                    elif status is None or status >= 400:
                        consecutive_errors += 1
                        if consecutive_errors >= 2:
                            reason = outcome = "REPEATED_ERRORS"
                            exit_code = 3
                            stop = True
                    else:
                        consecutive_errors = 0
    except BaseException as exc:
        error_class = type(exc).__name__
        reason = outcome = "EXCEPTION"
        exit_code = 4
        raise
    finally:
        ended_at = now()
        manifest = {
            "version": 1,
            "probe": "PUBLICCOM_VOLUME_LATENCY",
            "probe_version": PROBE_VERSION,
            "run_id": run_id,
            "started_at_utc": _iso(started_at),
            "ended_at_utc": _iso(ended_at),
            "plan": plan(),
            "setup": setup,
            "requests_total": len(setup) + len(requests),
            "setup_requests": len(setup),
            "bars_requests": bars_requests,
            "status_counts": status_counts,
            "latency_ms": _latency_summary(latencies),
            "shutdown_reason": reason,
            "outcome": outcome,
            "exit_code": exit_code,
            "error_class": error_class,
            "outputs": {"requests": "requests.jsonl", "manifest": "manifest.json"},
        }
        out.mkdir(parents=True, exist_ok=True)
        requests_path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in requests),
            encoding="utf-8")
        _atomic_json(manifest_path, manifest)
    return {
        "run_id": run_id,
        "outcome": outcome,
        "shutdown_reason": reason,
        "exit_code": exit_code,
        "requests_total": len(setup) + len(requests),
        "manifest_path": str(manifest_path),
        "requests_path": str(requests_path),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="make the bounded read-only requests (requires approval)")
    parser.add_argument("--output-dir", help="default: run_<run_id> beside this file")
    args = parser.parse_args(argv)

    if not args.execute:
        print(json.dumps(plan(), indent=2))
        return 0

    from public_api_test.client import PublicComClient

    client = PublicComClient(requests_per_minute=REQUESTS_PER_MINUTE)
    try:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = Path(args.output_dir) if args.output_dir else (
            RUN_ROOT / f"run_{run_id}")
        result = run_probe(client, output)
    finally:
        client.close()
    print(json.dumps({
        "run_id": result["run_id"],
        "outcome": result["outcome"],
        "shutdown_reason": result["shutdown_reason"],
        "requests_total": result["requests_total"],
        "manifest_path": result["manifest_path"],
        "requests_path": result["requests_path"],
    }, indent=2))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
