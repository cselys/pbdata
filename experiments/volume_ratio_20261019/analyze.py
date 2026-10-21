"""Analyze a Public.com volume-ratio experiment run (offline).

Reads the sanitized raw responses produced by collect.py for one run directory,
plus the signal project's production `minute_bars` (strictly read-only), and
emits analysis.json. No network access.

Usage:
    python experiments/volume_ratio_20261019/analyze.py <run_dir>
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROD_DB = Path("C:/Git/AI/Claude/realtimesignals/data/scanner.sqlite3")
SYMBOLS = ["SPY", "QQQ", "NVDA"]
BUCKETS = ["preMarket", "regularMarket", "afterMarket"]


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return dt.astimezone(ET)


def load_bars(raw: dict) -> dict:
    out = {}
    for bucket in BUCKETS:
        group = raw.get(bucket) or {}
        bars = group.get("bars") or []
        rows = []
        for b in bars:
            rows.append({
                "bucket": bucket,
                "start_et": parse_ts(b["timestamp"]),
                "raw_timestamp": b["timestamp"],
                "open": float(b["open"]),
                "close": float(b["close"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "volume": float(b["volume"]),
            })
        out[bucket] = rows
    return out


def interval_stats(bars: list) -> dict:
    vols = [b["volume"] for b in bars]
    diffs = [vols[i] - vols[i - 1] for i in range(1, len(vols))]
    decreases = sum(1 for d in diffs if d < 0)
    increases = sum(1 for d in diffs if d > 0)
    flat = sum(1 for d in diffs if d == 0)
    return {
        "bars": len(bars),
        "sum_volume": round(sum(vols), 6),
        "min_volume": min(vols) if vols else None,
        "max_volume": max(vols) if vols else None,
        "consecutive_increases": increases,
        "consecutive_decreases": decreases,
        "consecutive_flat": flat,
        "non_monotonic": decreases > 0,
        "cumulative_like": decreases == 0 and flat < len(diffs) * 0.05,
        "last_equals_sum": bool(vols) and abs(vols[-1] - sum(vols)) < 1e-6,
    }


def spacing_check(bars: list) -> dict:
    gaps = [(bars[i]["start_et"] - bars[i - 1]["start_et"]).total_seconds()
            for i in range(1, len(bars))]
    seconds = sorted(set(gaps))
    return {"intervals_seconds": seconds, "exactly_60s": seconds == [60.0]}


def spearman(xs: list, ys: list) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return None if den == 0 else num / den


def load_schwab_bars(symbol: str, day_et: str) -> dict:
    """Return {minute_start_utc: volume} from production minute_bars (read-only)."""
    if not PROD_DB.exists():
        return {}
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        rows = conn.execute(
            "SELECT timestamp, volume FROM minute_bars WHERE symbol=? ORDER BY timestamp",
            (symbol,),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for ts, vol in rows:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(ET).strftime("%Y-%m-%d") == day_et:
            out[dt.astimezone(timezone.utc)] = float(vol)
    return out


def trailing_ratio(public_minutes: dict, anchor_et: datetime, small=5, large=15):
    """5/15 ratio from Public 1-min interval volumes keyed by ET minute start."""
    def window(n):
        return [public_minutes.get(anchor_et - timedelta(minutes=k)) for k in range(n)]

    sm = window(small)
    lg = window(large)
    if any(v is None for v in sm + lg):
        return None, sm, lg, "MISSING_BAR"
    num, den = sum(sm), sum(lg)
    if den == 0:
        return None, num, den, "ZERO_DENOMINATOR"
    return round(num / den, 6), num, den, "OK"


def analyze(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    quotes_raw = json.loads((run_dir / "quotes_1.json").read_text())
    bars_raw = json.loads((run_dir / "bars_1min.json").read_text())

    quote_by_symbol = {}
    for q in quotes_raw["results"][0]["raw_response"]["quotes"]:
        sym = q["instrument"]["symbol"]
        quote_by_symbol[sym] = {
            "last": q.get("last"),
            "lastTimestamp": q.get("lastTimestamp"),
            "volume": q.get("volume"),
        }

    result = {
        "run_id": manifest["run_id"],
        "quote_snapshot": quote_by_symbol,
        "symbols": {},
        "cross_provider": {},
        "ratio_examples": {},
    }

    for sym in SYMBOLS:
        record = bars_raw[sym]
        if record.get("http_status") != 200 or not isinstance(record.get("raw_response"), dict):
            result["symbols"][sym] = {"status": "NO_BARS", "http_status": record.get("http_status")}
            continue
        buckets = load_bars(record["raw_response"])
        day_et = buckets["regularMarket"][0]["start_et"].strftime("%Y-%m-%d") if buckets["regularMarket"] else None
        per_bucket = {b: interval_stats(buckets[b]) for b in BUCKETS if buckets[b]}
        alignment = {
            b: {
                "first_et": buckets[b][0]["start_et"].isoformat(),
                "last_et": buckets[b][-1]["start_et"].isoformat(),
                "spacing": spacing_check(buckets[b]),
                "timezone_offsets": sorted({b2["raw_timestamp"][-6:] for b2 in buckets[b]}),
            }
            for b in BUCKETS if buckets[b]
        }
        total_all = sum(v["sum_volume"] for v in per_bucket.values())
        total_regular = per_bucket.get("regularMarket", {}).get("sum_volume", 0)
        total_pre = per_bucket.get("preMarket", {}).get("sum_volume", 0)
        qv = quote_by_symbol.get(sym, {}).get("volume")
        result["symbols"][sym] = {
            "status": "OK",
            "bucket_counts": {b: len(buckets[b]) for b in BUCKETS},
            "interval_stats": per_bucket,
            "time_alignment": alignment,
            "sum_premarket": round(total_pre, 6),
            "sum_regular": round(total_regular, 6),
            "sum_all_buckets": round(total_all, 6),
            "quote_volume": qv,
            "all_vs_quote_ratio": round(total_all / qv, 6) if qv else None,
            "regular_vs_quote_ratio": round(total_regular / qv, 6) if qv else None,
        }

        # Cross-provider regular-session minute comparison (Schwab).
        if day_et:
            schwab = load_schwab_bars(sym, day_et)
            pub = {b["start_et"].astimezone(timezone.utc): b["volume"]
                   for b in buckets["regularMarket"] if b["start_et"].minute < 60}
            common = sorted(set(pub) & set(schwab))
            diffs = [abs(pub[m] - schwab[m]) / schwab[m] for m in common if schwab[m] > 0]
            xs = [pub[m] for m in common]
            ys = [schwab[m] for m in common]
            result["cross_provider"][sym] = {
                "schwab_bars_for_day": len(schwab),
                "public_regular_bars": len(pub),
                "overlapping_minutes": len(common),
                "median_abs_relative_diff": round(statistics.median(diffs), 6) if diffs else None,
                "mean_abs_relative_diff": round(statistics.mean(diffs), 6) if diffs else None,
                "spearman": round(spearman(xs, ys), 6) if spearman(xs, ys) is not None else None,
                "public_sum_common": round(sum(xs), 6),
                "schwab_sum_common": round(sum(ys), 6),
                "sample_minutes": [
                    {"minute_utc": m.isoformat(), "public": pub[m], "schwab": schwab[m]}
                    for m in common[:3]
                ],
            }

        # Ratio examples anchored at selected ET minutes (same-day 1-min interval bars).
        public_minutes = {}
        for b in buckets["preMarket"] + buckets["regularMarket"]:
            public_minutes[b["start_et"]] = b["volume"]
        anchors = ["09:30", "09:35", "10:00", "12:00"]
        examples = {}
        for hhmm in anchors:
            h, m = map(int, hhmm.split(":"))
            anchor = datetime.combine(buckets["regularMarket"][0]["start_et"].date(),
                                      datetime.min.time(), ET).replace(hour=h, minute=m)
            ratio, num, den, flag = trailing_ratio(public_minutes, anchor)
            schwab_ratio = None
            if day_et:
                schwab = load_schwab_bars(sym, day_et)
                vals = []
                for k in range(15):
                    mins = anchor - timedelta(minutes=k)
                    utc = mins.astimezone(timezone.utc)
                    vals.append(schwab.get(utc))
                if all(v is not None for v in vals):
                    s_num = sum(vals[:5])
                    s_den = sum(vals)
                    schwab_ratio = round(s_num / s_den, 6) if s_den else None
            examples[hhmm] = {
                "public_ratio": ratio,
                "public_numerator": num,
                "public_denominator": den,
                "flag": flag,
                "schwab_ratio": schwab_ratio,
                "diff": round(ratio - schwab_ratio, 6)
                if ratio is not None and schwab_ratio is not None else None,
            }
        result["ratio_examples"][sym] = examples

    return result


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    run_dir = Path(sys.argv[1])
    analysis = analyze(run_dir)
    target = run_dir / "analysis.json"
    target.write_text(json.dumps(analysis, indent=2, sort_keys=True))
    print(json.dumps({
        "analysis": str(target),
        "symbols": {s: analysis["symbols"].get(s, {}).get("status")
                    for s in analysis["symbols"]},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
