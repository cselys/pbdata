"""Public.com volume-ratio experiment — bounded read-only collector.

Reuses the existing PublicComClient (auth, pacing, sanitization). See
PREREGISTRATION.md for the fixed request list and decision rules.

Run from the datasrc project root:
    python -m experiments.volume_ratio_20261019.collect
or:
    ./.venv/Scripts/python.exe experiments/volume_ratio_20261019/collect.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from public_api_test.client import PublicComClient  # noqa: E402

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUT = Path(__file__).resolve().parent / f"run_{RUN_ID}"
OUT.mkdir(parents=True, exist_ok=True)

SYMBOLS = ["SPY", "QQQ", "NVDA"]
TOGGLE = "REGULAR_AND_EXTENDED_HOURS"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_raw(name: str, payload) -> dict:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    (OUT / name).write_bytes(body)
    return {"file": name, "bytes": len(body), "sha256": sha256_bytes(body)}


def bars_endpoint(symbol: str) -> str:
    return f"/userapigateway/historicdata/EQUITY/{symbol}/DAY/ONE_MINUTE"


def main() -> int:
    client = PublicComClient(requests_per_minute=20)
    manifest = {
        "run_id": RUN_ID,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbols": SYMBOLS,
        "trading_session_toggle": TOGGLE,
        "requests_per_minute": 20,
        "planned_market_requests": 5,
        "note": "Public.com volume-ratio experiment; read-only; no orders",
        "requests": [],
        "raw_files": [],
    }
    try:
        setup = client.prepare()
        manifest["setup"] = setup
        if not client.token:
            manifest["status"] = "AUTH_UNAVAILABLE"
            (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
            print("AUTH_UNAVAILABLE")
            return 2

        # 1. quotes snapshot 1
        q1 = client.get_quotes_batch(SYMBOLS)
        manifest["raw_files"].append(write_raw("quotes_1.json", q1))
        manifest["requests"].append({"index": 1, "kind": "quotes_batch",
                                     "symbols": SYMBOLS, "category": q1.get("category"),
                                     "http_status": q1.get("http_status")})
        if q1.get("category") not in ("success", "partial_result"):
            manifest["status"] = "QUOTES_FAILED"
            (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
            print(json.dumps(manifest, indent=2))
            return 3

        # 2-4. one-minute bars per symbol
        bars_payload = {}
        for i, sym in enumerate(SYMBOLS, start=2):
            endpoint = bars_endpoint(sym)
            record = client._record(sym, endpoint, client._request(
                "GET", endpoint, params={"tradingSessionToggle": TOGGLE}),
                "historical_bars", "GET")
            record.update(data_kind="historical_bars_1min",
                          request_parameters={"tradingSessionToggle": TOGGLE,
                                              "aggregation": "ONE_MINUTE"})
            body = record.get("raw_response")
            if record.get("category") == "missing_price" and isinstance(body, dict) and any(
                    key in body for key in ("preMarket", "regularMarket", "afterMarket")):
                record["category"] = "success"
            bars_payload[sym] = record
            manifest["requests"].append({"index": i, "kind": "bars_1min", "symbol": sym,
                                         "category": record.get("category"),
                                         "http_status": record.get("http_status")})
        manifest["raw_files"].append(write_raw("bars_1min.json", bars_payload))
        if all(r.get("category") != "success" for r in bars_payload.values()):
            manifest["status"] = "BARS_FAILED"
            (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
            print(json.dumps(manifest, indent=2))
            return 4

        # 5. quotes snapshot 2
        q2 = client.get_quotes_batch(SYMBOLS)
        manifest["raw_files"].append(write_raw("quotes_2.json", q2))
        manifest["requests"].append({"index": 5, "kind": "quotes_batch",
                                     "symbols": SYMBOLS, "category": q2.get("category"),
                                     "http_status": q2.get("http_status")})

        manifest["status"] = "OK"
    finally:
        client.close()
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: manifest[k] for k in
                      ("run_id", "status", "started_at_utc", "finished_at_utc")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
