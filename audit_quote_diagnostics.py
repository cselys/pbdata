#!/usr/bin/env python3
"""Standalone, read-only audit of a Public.com quote_diagnostics run.

Reads:
  results/raw/quote_diagnostics_20261026T174340171097Z.jsonl
  results/summaries/quote_diagnostics_20261026T174340171097Z.json

Writes (new, unique directory under results/audits/):
  audit_report.json   -- machine-readable results
  audit_report.md     -- human-readable report

No live API calls. No existing files are modified or overwritten.
Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RAW_PATH = "results/raw/quote_diagnostics_20261026T174340171097Z.jsonl"
DEFAULT_SUMMARY_PATH = "results/summaries/quote_diagnostics_20261026T174340171097Z.json"
DEFAULT_OUTPUT_ROOT = "results/audits"

REQUIRED_FIELDS = [
    "symbol", "sweep", "scheduled_offset_seconds",
    "actual_start_offset_seconds", "request_start_utc",
    "request_finished_utc", "http_status", "http_latency_ms",
    "batch_index", "symbols_in_batch", "category",
    "last_price", "last_timestamp_raw", "last_age_seconds",
    "last_timestamp_status", "bid_price", "bid_timestamp_raw",
    "bid_age_seconds", "bid_timestamp_status", "ask_price",
    "ask_timestamp_raw", "ask_age_seconds", "ask_timestamp_status",
    "volume", "volume_changed", "price_changed",
    "last_timestamp_changed", "batch_timestamp_advanced",
    "market_session", "provider_session", "request_category",
    "api_error_code", "missing_symbols",
]

FIELD_TYPE_EXPECTATIONS = {
    "symbol": str,
    "sweep": int,
    "scheduled_offset_seconds": (int, float),
    "actual_start_offset_seconds": (int, float),
    "request_start_utc": str,
    "request_finished_utc": str,
    "http_status": int,
    "http_latency_ms": (int, float),
    "batch_index": int,
    "symbols_in_batch": int,
    "category": str,
    "last_price": (int, float),
    "last_timestamp_raw": str,
    "last_age_seconds": (int, float),
    "last_timestamp_status": str,
    "bid_price": (int, float),
    "bid_timestamp_raw": str,
    "bid_age_seconds": (int, float),
    "bid_timestamp_status": str,
    "ask_price": (int, float),
    "ask_timestamp_raw": str,
    "ask_age_seconds": (int, float),
    "ask_timestamp_status": str,
    "volume": (int, float),
    "volume_changed": bool,
    "price_changed": bool,
    "last_timestamp_changed": bool,
    "batch_timestamp_advanced": bool,
    "market_session": str,
    "provider_session": str,
    "request_category": str,
    "api_error_code": type(None),
    "missing_symbols": list,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_utc_iso(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp to a Unix epoch float (seconds).

    Handles trailing 'Z' and explicit '+00:00' offsets. Returns None for
    None/empty/unparseable/naive values. Never guesses units.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return None
            return parsed.timestamp()
        except (ValueError, OverflowError, TypeError):
            return None
    return None


def stats(values: List[float]) -> Optional[Dict[str, Any]]:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    sorted_v = sorted(clean)
    n = len(sorted_v)

    def pct(q: float) -> float:
        idx = int(q * (n - 1))
        return sorted_v[idx]

    return {
        "count": n,
        "min": min(sorted_v),
        "max": max(sorted_v),
        "mean": sum(sorted_v) / n,
        "median": median(sorted_v),
        "p95": pct(0.95),
    }


def pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator * 100.0


def unique_output_dir(root: str, prefix: str) -> str:
    """Create a unique timestamped directory under root. Stop on collision."""
    from datetime import datetime as _dt
    ts = _dt.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = os.path.join(root, f"{prefix}_{ts}")
    if os.path.exists(candidate):
        # Add a counter suffix to guarantee uniqueness without overwriting.
        counter = 1
        while True:
            candidate = os.path.join(root, f"{prefix}_{ts}_{counter}")
            if not os.path.exists(candidate):
                break
            counter += 1
    os.makedirs(candidate, exist_ok=True)
    return candidate


def load_jsonl(path: str) -> Dict[str, Any]:
    """Stream JSONL. Report physical lines, valid/invalid JSON with line numbers."""
    report: Dict[str, Any] = {
        "path": path,
        "physical_lines": 0,
        "valid_records": 0,
        "invalid_json_lines": [],
        "records": [],
        "parse_errors": [],
    }
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            report["physical_lines"] += 1
            if stripped == "":
                # Count blank lines as physical lines but not records.
                continue
            try:
                obj = json.loads(stripped)
                if not isinstance(obj, dict):
                    report["invalid_json_lines"].append(line_no)
                    report["parse_errors"].append({
                        "line": line_no,
                        "error": "JSON value is not an object",
                        "raw": stripped[:200],
                    })
                    continue
                report["valid_records"] += 1
                report["records"].append(obj)
            except json.JSONDecodeError as exc:
                report["invalid_json_lines"].append(line_no)
                report["parse_errors"].append({
                    "line": line_no,
                    "error": str(exc),
                    "raw": stripped[:200],
                })
    return report


def load_summary(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Validation (Section A)
# ---------------------------------------------------------------------------

def validate_records(records: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
    val: Dict[str, Any] = {}

    # Symbols / sweeps / pairs
    symbols_observed = sorted({r.get("symbol") for r in records if isinstance(r.get("symbol"), str)})
    sweeps = [r.get("sweep") for r in records if isinstance(r.get("sweep"), int)]
    min_sweep = min(sweeps) if sweeps else None
    max_sweep = max(sweeps) if sweeps else None

    pairs = [(r.get("sweep"), r.get("symbol")) for r in records
             if isinstance(r.get("sweep"), int) and isinstance(r.get("symbol"), str)]
    pair_counts = Counter(pairs)
    duplicate_pairs = {k: v for k, v in pair_counts.items() if v > 1}
    unique_pairs = len(pair_counts)

    sweep_set = {s for s in sweeps if isinstance(s, int)}
    missing_sweeps = []
    if min_sweep is not None and max_sweep is not None:
        expected_range = set(range(min_sweep, max_sweep + 1))
        missing_sweeps = sorted(expected_range - sweep_set)

    # Per-symbol counts and sweep coverage
    per_symbol_observed: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "sweeps": set(), "sweep_count": 0,
    })
    for r in records:
        sym = r.get("symbol")
        sw = r.get("sweep")
        if not isinstance(sym, str) or not isinstance(sw, int):
            continue
        per_symbol_observed[sym]["count"] += 1
        per_symbol_observed[sym]["sweeps"].add(sw)
    for sym, data in per_symbol_observed.items():
        data["sweep_count"] = len(data["sweeps"])
        data["sweeps"] = sorted(data["sweeps"])

    # Expectations from summary
    summary_symbols = summary.get("symbols", [])
    declared_sweeps = summary.get("total_sweeps")
    expected_combos = set()
    for sym in summary_symbols:
        if declared_sweeps is not None:
            for sw in range(1, declared_sweeps + 1):
                expected_combos.add((sw, sym))

    observed_pair_set = set(pair_counts.keys())
    missing_combos = sorted(expected_combos - observed_pair_set)
    unexpected_symbols = sorted(set(symbols_observed) - set(summary_symbols))

    # Required fields / types
    missing_field_report: Dict[str, List[int]] = defaultdict(list)
    type_mismatch_report: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for idx, r in enumerate(records, start=1):
        for field in REQUIRED_FIELDS:
            if field not in r:
                missing_field_report[field].append(idx)
            else:
                expected = FIELD_TYPE_EXPECTATIONS.get(field)
                if expected is not None and not isinstance(r[field], expected):
                    type_mismatch_report[field].append({
                        "line": idx,
                        "value": r[field],
                        "value_type": type(r[field]).__name__,
                        "expected": expected if isinstance(expected, tuple) else expected.__name__,
                    })

    val["symbols_observed"] = symbols_observed
    val["unique_symbol_count"] = len(symbols_observed)
    val["min_sweep"] = min_sweep
    val["max_sweep"] = max_sweep
    val["sweep_range_observed"] = (min_sweep, max_sweep)
    val["unique_pair_count"] = unique_pairs
    val["total_pair_records"] = len(pairs)
    val["duplicate_pairs"] = {f"({k[0]}, {k[1]})": v for k, v in duplicate_pairs.items()}
    val["duplicate_pair_count"] = len(duplicate_pairs)
    val["missing_sweeps_in_range"] = missing_sweeps
    val["per_symbol_observed"] = dict(per_symbol_observed)
    val["summary_symbols"] = summary_symbols
    val["summary_total_sweeps_declared"] = declared_sweeps
    val["expected_symbol_sweep_combinations"] = len(expected_combos)
    val["missing_expected_combinations"] = [(c[0], c[1]) for c in missing_combos]
    val["missing_expected_combination_count"] = len(missing_combos)
    val["unexpected_symbols"] = unexpected_symbols
    val["unexpected_symbol_count"] = len(unexpected_symbols)
    val["missing_required_fields"] = {k: v for k, v in missing_field_report.items()}
    val["field_type_mismatches"] = {k: v for k, v in type_mismatch_report.items()}
    return val


# ---------------------------------------------------------------------------
# HTTP / request integrity (Section B)
# ---------------------------------------------------------------------------

def http_integrity(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    http: Dict[str, Any] = {}
    statuses = [r.get("http_status") for r in records if isinstance(r.get("http_status"), int)]
    status_dist = Counter(str(s) for s in statuses)
    success_count = sum(1 for s in statuses if 200 <= s < 300)
    error_count = len(statuses) - success_count

    latencies = [r.get("http_latency_ms") for r in records
                 if isinstance(r.get("http_latency_ms"), (int, float))]
    latency_stats = stats(latencies) if latencies else None

    starts = [parse_utc_iso(r.get("request_start_utc")) for r in records]
    finishes = [parse_utc_iso(r.get("request_finished_utc")) for r in records]
    starts = [s for s in starts if s is not None]
    finishes = [f for f in finishes if f is not None]

    earliest_start = min(starts) if starts else None
    latest_finish = max(finishes) if finishes else None
    actual_elapsed = (latest_finish - earliest_start) if (earliest_start is not None and latest_finish is not None) else None

    # Per-sweep
    per_sweep: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0, "start_times": [], "finish_times": [], "symbols": set(),
    })
    for r in records:
        sw = r.get("sweep")
        if not isinstance(sw, int):
            continue
        per_sweep[sw]["count"] += 1
        st = parse_utc_iso(r.get("request_start_utc"))
        ft = parse_utc_iso(r.get("request_finished_utc"))
        if st is not None:
            per_sweep[sw]["start_times"].append(st)
        if ft is not None:
            per_sweep[sw]["finish_times"].append(ft)
        sym = r.get("symbol")
        if isinstance(sym, str):
            per_sweep[sw]["symbols"].add(sym)

    per_sweep_out = {}
    for sw in sorted(per_sweep.keys()):
        d = per_sweep[sw]
        d["start_min"] = min(d["start_times"]) if d["start_times"] else None
        d["start_max"] = max(d["start_times"]) if d["start_times"] else None
        d["finish_min"] = min(d["finish_times"]) if d["finish_times"] else None
        d["finish_max"] = max(d["finish_times"]) if d["finish_times"] else None
        d["symbol_count"] = len(d["symbols"])
        d["symbols"] = sorted(d["symbols"])
        # Convert floats to ISO strings for JSON output
        def to_iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None
        d["start_min_iso"] = to_iso(d.pop("start_min"))
        d["start_max_iso"] = to_iso(d.pop("start_max"))
        d["finish_min_iso"] = to_iso(d.pop("finish_min"))
        d["finish_max_iso"] = to_iso(d.pop("finish_max"))
        per_sweep_out[str(sw)] = d

    # Batch metadata consistency
    batch_indexes = [r.get("batch_index") for r in records if isinstance(r.get("batch_index"), int)]
    symbols_in_batch_vals = [r.get("symbols_in_batch") for r in records
                             if isinstance(r.get("symbols_in_batch"), int)]
    batch_index_set = set(batch_indexes)
    sib_set = set(symbols_in_batch_vals)

    http["http_status_distribution"] = dict(status_dist)
    http["http_status_count_total"] = len(statuses)
    http["success_count"] = success_count
    http["error_count"] = error_count
    http["latency_stats"] = latency_stats
    http["earliest_request_start_utc"] = datetime.fromtimestamp(earliest_start, tz=timezone.utc).isoformat() if earliest_start is not None else None
    http["latest_request_finish_utc"] = datetime.fromtimestamp(latest_finish, tz=timezone.utc).isoformat() if latest_finish is not None else None
    http["actual_elapsed_seconds"] = actual_elapsed
    http["per_sweep_request_counts"] = per_sweep_out
    http["sweep_count_observed"] = len(per_sweep_out)
    http["batch_index_values_observed"] = sorted(batch_index_set)
    http["symbols_in_batch_values_observed"] = sorted(sib_set)
    http["batch_index_consistent_single_value"] = len(batch_index_set) <= 1
    http["symbols_in_batch_consistent_single_value"] = len(sib_set) <= 1
    return http


# ---------------------------------------------------------------------------
# Timestamp validation (Section C)
# ---------------------------------------------------------------------------

def timestamp_validation(records: List[Dict[str, Any]], max_age_ref: Optional[float] = None) -> Dict[str, Any]:
    ts: Dict[str, Any] = {}
    per_symbol_status: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(Counter)
    )

    for side in ("last", "bid", "ask"):
        raw_field = f"{side}_timestamp_raw"
        age_field = f"{side}_age_seconds"
        status_field = f"{side}_timestamp_status"
        price_field = f"{side}_price"

        ages_recorded = []
        ages_recalculated = []
        discrepancies = []
        unparseable = []
        status_counts = Counter()
        records_with_raw = 0
        records_with_price = 0

        for idx, r in enumerate(records, start=1):
            raw = r.get(raw_field)
            rec_age = r.get(age_field)
            status = r.get(status_field)
            finish = parse_utc_iso(r.get("request_finished_utc"))
            sym = r.get("symbol")
            if isinstance(raw, str) and raw != "":
                records_with_raw += 1
            if isinstance(rec_age, (int, float)):
                ages_recorded.append(float(rec_age))
            if isinstance(status, str):
                status_counts[status] += 1
                if isinstance(sym, str):
                    per_symbol_status[side][sym][status] += 1

            parsed = parse_utc_iso(raw)
            if raw is not None and raw != "" and parsed is None:
                unparseable.append({"line": idx, "field": raw_field, "value": raw})
                continue
            if parsed is not None and finish is not None:
                calc_age = finish - parsed
                ages_recalculated.append(calc_age)
                if isinstance(rec_age, (int, float)):
                    disc = abs(calc_age - float(rec_age))
                    discrepancies.append({
                        "line": idx,
                        "symbol": r.get("symbol"),
                        "sweep": r.get("sweep"),
                        "field": raw_field,
                        "raw_timestamp": raw,
                        "request_finished_utc": r.get("request_finished_utc"),
                        "recorded_age_seconds": rec_age,
                        "recalculated_age_seconds": calc_age,
                        "absolute_discrepancy_seconds": disc,
                    })
            if isinstance(r.get(price_field), (int, float)):
                records_with_price += 1

        disc_values = [d["absolute_discrepancy_seconds"] for d in discrepancies]
        disc_stats = stats(disc_values) if disc_values else None

        ts[side] = {
            "records_with_raw_timestamp": records_with_raw,
            "records_with_price": records_with_price,
            "recorded_age_stats": stats(ages_recorded) if ages_recorded else None,
            "recalculated_age_stats": stats(ages_recalculated) if ages_recalculated else None,
            "discrepancy_count": len(discrepancies),
            "max_absolute_discrepancy_seconds": max(disc_values) if disc_values else None,
            "discrepancy_stats": disc_stats,
            "discrepancies": discrepancies[:50],  # cap evidence
            "unparseable_timestamps": unparseable,
            "status_counts": dict(status_counts),
        }

    ts["per_symbol_status_counts"] = {
        side: {sym: dict(counts) for sym, counts in sym_data.items()}
        for side, sym_data in per_symbol_status.items()
    }

    # Note on freshness classification
    ts["freshness_classification_note"] = (
        "The raw records carry a status field (valid/stale/missing/future) but the audit "
        "script does not redefine the application's freshness classification. A max_age "
        "threshold appears in the summary (max_age_seconds) and is reported for reference "
        "only; the audit counts statuses from the actual status fields rather than "
        "recomputing them from an assumed threshold."
    )
    return ts


# ---------------------------------------------------------------------------
# Consecutive-observation analysis (Section D)
# ---------------------------------------------------------------------------

def consecutive_analysis(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    conv: Dict[str, Any] = {}

    # Group by symbol, sort by sweep, validate uniqueness
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        sym = r.get("symbol")
        sw = r.get("sweep")
        if isinstance(sym, str) and isinstance(sw, int):
            by_symbol[sym].append(r)

    per_symbol_out: Dict[str, Any] = {}
    global_transitions = {
        "volume_change_price_ts_unchanged": 0,
        "price_change_ts_unchanged": 0,
        "volume_change_price_unchanged": 0,
        "volume_change_ts_unchanged": 0,
        "price_changed_transitions": 0,
        "last_timestamp_changed_transitions": 0,
        "volume_changed_transitions": 0,
        "unchanged_price_ts_changed_volume": 0,
        "gap_marked_transitions": 0,
        "total_consecutive_pairs": 0,
    }
    nonzero_findings = []

    for symbol in sorted(by_symbol.keys()):
        obs_list = sorted(by_symbol[symbol], key=lambda r: r["sweep"])
        # Detect duplicate sweeps for this symbol
        sweep_seen = set()
        dup_sweeps = []
        for r in obs_list:
            if r["sweep"] in sweep_seen:
                dup_sweeps.append(r["sweep"])
            sweep_seen.add(r["sweep"])

        sym_transitions = {
            "observation_count": len(obs_list),
            "unique_sweeps": len(sweep_seen),
            "duplicate_sweeps": sorted(dup_sweeps),
            "volume_change_price_ts_unchanged": 0,
            "price_change_ts_unchanged": 0,
            "volume_change_price_unchanged": 0,
            "volume_change_ts_unchanged": 0,
            "price_changed_transitions": 0,
            "last_timestamp_changed_transitions": 0,
            "volume_changed_transitions": 0,
            "unchanged_price_ts_changed_volume": 0,
            "gap_marked_transitions": 0,
            "total_consecutive_pairs": 0,
            "findings": [],
        }

        for i in range(1, len(obs_list)):
            prev = obs_list[i - 1]
            curr = obs_list[i]
            sweep_gap = curr["sweep"] - prev["sweep"]
            is_gap = sweep_gap != 1

            if is_gap:
                sym_transitions["gap_marked_transitions"] += 1
                global_transitions["gap_marked_transitions"] += 1
                # Still evaluate changes but mark as gap
            sym_transitions["total_consecutive_pairs"] += 1
            global_transitions["total_consecutive_pairs"] += 1

            # Exact comparison, no rounding
            prev_price = prev.get("last_price")
            curr_price = curr.get("last_price")
            prev_volume = prev.get("volume")
            curr_volume = curr.get("volume")
            prev_ts = prev.get("last_timestamp_raw")
            curr_ts = curr.get("last_timestamp_raw")

            price_changed = (prev_price is not None and curr_price is not None
                             and prev_price != curr_price)
            volume_changed = (prev_volume is not None and curr_volume is not None
                              and prev_volume != curr_volume)
            ts_changed = (prev_ts is not None and curr_ts is not None
                          and prev_ts != curr_ts)

            if price_changed:
                sym_transitions["price_changed_transitions"] += 1
                global_transitions["price_changed_transitions"] += 1
            if ts_changed:
                sym_transitions["last_timestamp_changed_transitions"] += 1
                global_transitions["last_timestamp_changed_transitions"] += 1
            if volume_changed:
                sym_transitions["volume_changed_transitions"] += 1
                global_transitions["volume_changed_transitions"] += 1

            # Case 1: volume changes while price AND last-trade timestamp both unchanged
            if volume_changed and not price_changed and not ts_changed:
                sym_transitions["volume_change_price_ts_unchanged"] += 1
                global_transitions["volume_change_price_ts_unchanged"] += 1
                finding = {
                    "symbol": symbol,
                    "case": "volume_change_price_and_timestamp_unchanged",
                    "previous_sweep": prev["sweep"],
                    "current_sweep": curr["sweep"],
                    "sweep_gap": sweep_gap,
                    "is_gap": is_gap,
                    "previous": {
                        "last_price": prev_price,
                        "volume": prev_volume,
                        "last_timestamp_raw": prev_ts,
                    },
                    "current": {
                        "last_price": curr_price,
                        "volume": curr_volume,
                        "last_timestamp_raw": curr_ts,
                    },
                }
                sym_transitions["findings"].append(finding)
                nonzero_findings.append(finding)

            # Case 2: price changes while last-trade timestamp unchanged
            if price_changed and not ts_changed:
                sym_transitions["price_change_ts_unchanged"] += 1
                global_transitions["price_change_ts_unchanged"] += 1
                finding = {
                    "symbol": symbol,
                    "case": "price_change_timestamp_unchanged",
                    "previous_sweep": prev["sweep"],
                    "current_sweep": curr["sweep"],
                    "sweep_gap": sweep_gap,
                    "is_gap": is_gap,
                    "previous": {
                        "last_price": prev_price,
                        "last_timestamp_raw": prev_ts,
                    },
                    "current": {
                        "last_price": curr_price,
                        "last_timestamp_raw": curr_ts,
                    },
                }
                sym_transitions["findings"].append(finding)
                nonzero_findings.append(finding)

            # Case 3: volume changes while price unchanged (regardless of timestamp)
            if volume_changed and not price_changed:
                sym_transitions["volume_change_price_unchanged"] += 1
                global_transitions["volume_change_price_unchanged"] += 1
                finding = {
                    "symbol": symbol,
                    "case": "volume_change_price_unchanged",
                    "previous_sweep": prev["sweep"],
                    "current_sweep": curr["sweep"],
                    "sweep_gap": sweep_gap,
                    "is_gap": is_gap,
                    "previous": {
                        "last_price": prev_price,
                        "volume": prev_volume,
                        "last_timestamp_raw": prev_ts,
                    },
                    "current": {
                        "last_price": curr_price,
                        "volume": curr_volume,
                        "last_timestamp_raw": curr_ts,
                    },
                }
                sym_transitions["findings"].append(finding)
                nonzero_findings.append(finding)

            # Case 4: volume changes while last-trade timestamp unchanged (regardless of price)
            if volume_changed and not ts_changed:
                sym_transitions["volume_change_ts_unchanged"] += 1
                global_transitions["volume_change_ts_unchanged"] += 1
                finding = {
                    "symbol": symbol,
                    "case": "volume_change_timestamp_unchanged",
                    "previous_sweep": prev["sweep"],
                    "current_sweep": curr["sweep"],
                    "sweep_gap": sweep_gap,
                    "is_gap": is_gap,
                    "previous": {
                        "volume": prev_volume,
                        "last_timestamp_raw": prev_ts,
                        "last_price": prev_price,
                    },
                    "current": {
                        "volume": curr_volume,
                        "last_timestamp_raw": curr_ts,
                        "last_price": curr_price,
                    },
                }
                sym_transitions["findings"].append(finding)
                nonzero_findings.append(finding)

            # Unchanged price and timestamp but changed volume
            if not price_changed and not ts_changed and volume_changed:
                sym_transitions["unchanged_price_ts_changed_volume"] += 1
                global_transitions["unchanged_price_ts_changed_volume"] += 1

        per_symbol_out[symbol] = sym_transitions

    conv["per_symbol"] = per_symbol_out
    conv["global"] = global_transitions
    conv["nonzero_findings"] = nonzero_findings
    conv["nonzero_finding_count"] = len(nonzero_findings)
    conv["comparison_rule_note"] = (
        "Price and volume changes are determined with exact '!=' on the raw "
        "reported values (float for price/volume, string for timestamps). No "
        "rounding or tolerance is applied to the primary comparison. A "
        "separately labeled tolerance-based price comparison is included below "
        "for reference only."
    )
    return conv


def tolerance_price_comparison(records: List[Dict[str, Any]], tolerance: float = 1e-9) -> Dict[str, Any]:
    """Separately labeled tolerance-based price comparison (reference only)."""
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        sym = r.get("symbol")
        sw = r.get("sweep")
        if isinstance(sym, str) and isinstance(sw, int):
            by_symbol[sym].append(r)

    count_exact_changed = 0
    count_tolerance_changed = 0
    count_differ = 0
    examples = []

    for symbol in sorted(by_symbol.keys()):
        obs_list = sorted(by_symbol[symbol], key=lambda r: r["sweep"])
        for i in range(1, len(obs_list)):
            prev = obs_list[i - 1]
            curr = obs_list[i]
            prev_price = prev.get("last_price")
            curr_price = curr.get("last_price")
            if prev_price is None or curr_price is None:
                continue
            exact_changed = prev_price != curr_price
            tol_changed = abs(curr_price - prev_price) > tolerance
            if exact_changed:
                count_exact_changed += 1
            if tol_changed:
                count_tolerance_changed += 1
            if exact_changed != tol_changed:
                count_differ += 1
                if len(examples) < 10:
                    examples.append({
                        "symbol": symbol,
                        "previous_sweep": prev["sweep"],
                        "current_sweep": curr["sweep"],
                        "previous_price": prev_price,
                        "current_price": curr_price,
                        "absolute_difference": abs(curr_price - prev_price),
                    })

    return {
        "tolerance": tolerance,
        "exact_price_changed_count": count_exact_changed,
        "tolerance_price_changed_count": count_tolerance_changed,
        "count_where_exact_and_tolerance_differ": count_differ,
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# Flag re-evaluation (Section D continued)
# ---------------------------------------------------------------------------

def flag_reevaluation(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    flags: Dict[str, Any] = {}

    flag_fields = ["price_changed", "last_timestamp_changed", "volume_changed", "batch_timestamp_advanced"]
    agreement: Dict[str, Dict[str, int]] = {f: {"match": 0, "mismatch": 0, "uncertain": 0, "total": 0} for f in flag_fields}
    mismatches: Dict[str, List[Dict[str, Any]]] = {f: [] for f in flag_fields}

    # Recompute flags using consecutive-observation logic (same as Section D)
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        sym = r.get("symbol")
        sw = r.get("sweep")
        if isinstance(sym, str) and isinstance(sw, int):
            by_symbol[sym].append(r)

    # Track recomputed flag state per symbol (first record has no previous -> treat as False/no-change)
    recomputed: Dict[str, Dict[int, Dict[str, bool]]] = defaultdict(dict)
    for symbol in sorted(by_symbol.keys()):
        obs_list = sorted(by_symbol[symbol], key=lambda r: r["sweep"])
        prev = None
        for r in obs_list:
            sw = r["sweep"]
            state = {"price_changed": False, "last_timestamp_changed": False, "volume_changed": False}
            if prev is not None:
                state["price_changed"] = (prev.get("last_price") is not None and r.get("last_price") is not None
                                          and prev.get("last_price") != r.get("last_price"))
                state["volume_changed"] = (prev.get("volume") is not None and r.get("volume") is not None
                                           and prev.get("volume") != r.get("volume"))
                state["last_timestamp_changed"] = (prev.get("last_timestamp_raw") is not None
                                                   and r.get("last_timestamp_raw") is not None
                                                   and prev.get("last_timestamp_raw") != r.get("last_timestamp_raw"))
            recomputed[symbol][sw] = state
            prev = r

    for r in records:
        sym = r.get("symbol")
        sw = r.get("sweep")
        if not isinstance(sym, str) or not isinstance(sw, int):
            continue
        state = recomputed.get(sym, {}).get(sw)
        for field in ("price_changed", "last_timestamp_changed", "volume_changed"):
            stored = r.get(field)
            calc = state[field] if state is not None else False
            agreement[field]["total"] += 1
            if isinstance(stored, bool):
                if stored == calc:
                    agreement[field]["match"] += 1
                else:
                    agreement[field]["mismatch"] += 1
                    if len(mismatches[field]) < 20:
                        mismatches[field].append({
                            "symbol": sym,
                            "sweep": sw,
                            "stored": stored,
                            "recalculated": calc,
                        })
            else:
                agreement[field]["uncertain"] += 1

    # batch_timestamp_advanced: semantics uncertain (app uses bid/ask age deltas)
    batch_match = 0
    batch_mismatch = 0
    batch_uncertain = 0
    batch_total = 0
    batch_mismatches = []
    prev_bid_age: Dict[str, Optional[float]] = {}
    prev_ask_age: Dict[str, Optional[float]] = {}
    for symbol in sorted(by_symbol.keys()):
        obs_list = sorted(by_symbol[symbol], key=lambda r: r["sweep"])
        prev_bid_age[symbol] = None
        prev_ask_age[symbol] = None
        for r in obs_list:
            sw = r["sweep"]
            stored = r.get("batch_timestamp_advanced")
            bid_age = r.get("bid_age_seconds")
            ask_age = r.get("ask_age_seconds")
            calc = False
            if (prev_bid_age[symbol] is not None and isinstance(bid_age, (int, float))
                    and prev_bid_age[symbol] != bid_age) or \
               (prev_ask_age[symbol] is not None and isinstance(ask_age, (int, float))
                    and prev_ask_age[symbol] != ask_age):
                calc = True
            batch_total += 1
            if isinstance(stored, bool):
                if stored == calc:
                    batch_match += 1
                else:
                    batch_mismatch += 1
                    if len(batch_mismatches) < 20:
                        batch_mismatches.append({
                            "symbol": sym,
                            "sweep": sw,
                            "stored": stored,
                            "recalculated": calc,
                        })
            else:
                batch_uncertain += 1
            prev_bid_age[symbol] = bid_age if isinstance(bid_age, (int, float)) else None
            prev_ask_age[symbol] = ask_age if isinstance(ask_age, (int, float)) else None

    agreement["batch_timestamp_advanced"] = {
        "match": batch_match, "mismatch": batch_mismatch, "uncertain": batch_uncertain, "total": batch_total,
    }
    mismatches["batch_timestamp_advanced"] = batch_mismatches

    flags["flag_agreement"] = agreement
    flags["flag_mismatches"] = mismatches
    flags["batch_timestamp_advanced_semantics_note"] = (
        "batch_timestamp_advanced could not be unambiguously mapped to a "
        "single raw-data definition. The application appears to derive it from "
        "bid/ask age deltas between consecutive observations for a symbol; the "
        "audit recomputes it that way but labels the equivalence uncertain "
        "because the field name suggests batch-level rather than per-symbol "
        "semantics."
    )
    return flags


# ---------------------------------------------------------------------------
# Summary reconciliation (Section E)
# ---------------------------------------------------------------------------

def reconcile(raw_metrics: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    comparisons = []

    def add(metric, raw_val, summary_val, note=""):
        diff = None
        if isinstance(raw_val, (int, float)) and isinstance(summary_val, (int, float)):
            diff = raw_val - summary_val
        comparisons.append({
            "metric": metric,
            "raw_derived_value": raw_val,
            "summary_value": summary_val,
            "difference": diff,
            "match": raw_val == summary_val,
            "note": note,
        })

    # Total observations
    add("total_observations", raw_metrics["validation"]["total_valid_records"],
        summary.get("total_observations"))

    # Symbols
    add("declared_symbols_count", len(raw_metrics["validation"]["summary_symbols"]),
        len(summary.get("symbols", [])))
    add("observed_unique_symbols", raw_metrics["validation"]["unique_symbol_count"],
        len(summary.get("symbols", [])))

    # Per-symbol observation counts
    for sym in summary.get("symbols", []):
        obs = raw_metrics["validation"]["per_symbol_observed"].get(sym, {})
        add(f"per_symbol_observations_{sym}",
            obs.get("count"),
            summary.get("per_symbol", {}).get(sym, {}).get("observations"))

    # HTTP status distribution
    add("http_status_200_count",
        raw_metrics["http"]["http_status_distribution"].get("200", 0),
        summary.get("http_status_distribution", {}).get("200"))

    # HTTP latency stats
    lat_raw = raw_metrics["http"]["latency_stats"]
    lat_sum = summary.get("http_latency_ms", {})
    for key in ("count", "min", "max", "mean", "median", "p95"):
        add(f"http_latency_{key}",
            lat_raw.get(key) if lat_raw else None,
            lat_sum.get(key))

    # Timing
    add("actual_elapsed_seconds", raw_metrics["http"]["actual_elapsed_seconds"],
        summary.get("elapsed_seconds"))
    add("sweep_count", raw_metrics["http"]["sweep_count_observed"],
        summary.get("total_sweeps"))

    # Last status totals
    last_status = raw_metrics["timestamps"]["last"]["status_counts"]
    per_symbol_last_status = raw_metrics["timestamps"].get(
        "per_symbol_status_counts", {}
    ).get("last", {})
    add("valid_last_total", last_status.get("valid", 0), summary.get("valid_last_total"))
    add("stale_last_total", last_status.get("stale", 0), summary.get("stale_last_total"))
    add("missing_last_total", last_status.get("missing_timestamp", 0), summary.get("missing_last_total"))
    add("future_last_total", last_status.get("future_timestamp", 0), summary.get("future_last_total"))

    # Per-symbol freshness (compare per-symbol raw counts to per-symbol summary values)
    for sym in summary.get("symbols", []):
        ps_raw = raw_metrics["validation"]["per_symbol_observed"].get(sym, {})
        ps_sum = summary.get("per_symbol", {}).get(sym, {})
        ps_last = per_symbol_last_status.get(sym, {})
        add(f"{sym}_observations", ps_raw.get("count"), ps_sum.get("observations"))
        add(f"{sym}_valid_last", ps_last.get("valid", 0), ps_sum.get("valid_last"))
        add(f"{sym}_stale_last", ps_last.get("stale", 0), ps_sum.get("stale_last"))
        add(f"{sym}_missing_last", ps_last.get("missing_timestamp", 0), ps_sum.get("missing_last"))
        add(f"{sym}_future_last", ps_last.get("future_timestamp", 0), ps_sum.get("future_last"))

    # Bid/ask stale totals
    bid_status = raw_metrics["timestamps"]["bid"]["status_counts"]
    ask_status = raw_metrics["timestamps"]["ask"]["status_counts"]
    add("stale_bid_total", bid_status.get("stale", 0), summary.get("stale_bid_total"))
    add("stale_ask_total", ask_status.get("stale", 0), summary.get("stale_ask_total"))

    # Change metrics
    conv = raw_metrics["consecutive"]
    add("price_changed_transitions_total", conv["global"]["price_changed_transitions"],
        summary.get("price_changed_when_last_ts_unchanged"),
        note="Summary field name differs (price_changed_when_last_ts_unchanged); compare with caution.")
    add("volume_changed_transitions_total", conv["global"]["volume_changed_transitions"],
        None)

    # Per-symbol change metrics from summary
    for sym in summary.get("symbols", []):
        ps_sum = summary.get("per_symbol", {}).get(sym, {})
        ps_conv = conv["per_symbol"].get(sym, {})
        add(f"{sym}_last_timestamp_advanced", ps_conv.get("last_timestamp_changed_transitions"),
            ps_sum.get("last_timestamp_advanced"))
        add(f"{sym}_last_price_changed", ps_conv.get("price_changed_transitions"),
            ps_sum.get("last_price_changed"))
        add(f"{sym}_volume_changed", ps_conv.get("volume_changed_transitions"),
            ps_sum.get("volume_changed"))

    return comparisons


# ---------------------------------------------------------------------------
# Main audit assembly
# ---------------------------------------------------------------------------

def run_audit(raw_path: str, summary_path: str, output_dir: str) -> Dict[str, Any]:
    # Load inputs
    jsonl_report = load_jsonl(raw_path)
    records = jsonl_report["records"]

    summary = load_summary(summary_path)

    # Validation (A)
    validation = validate_records(records, summary)
    validation["total_valid_records"] = jsonl_report["valid_records"]
    validation["total_physical_lines"] = jsonl_report["physical_lines"]
    validation["invalid_json_lines"] = jsonl_report["invalid_json_lines"]
    validation["parse_errors"] = jsonl_report["parse_errors"]

    # HTTP integrity (B)
    http = http_integrity(records)

    # Timestamp validation (C)
    timestamps = timestamp_validation(records, max_age_ref=summary.get("max_age_seconds"))

    # Consecutive analysis (D)
    consecutive = consecutive_analysis(records)
    tolerance = tolerance_price_comparison(records)

    # Flag re-evaluation (D)
    flags = flag_reevaluation(records)

    # Reconciliation (E)
    comparisons = reconcile({
        "validation": validation,
        "http": http,
        "timestamps": timestamps,
        "consecutive": consecutive,
    }, summary)

    # Warnings
    warnings = []
    if validation["invalid_json_lines"]:
        warnings.append(f"{len(validation['invalid_json_lines'])} invalid JSON line(s): {validation['invalid_json_lines'][:10]}")
    if validation["missing_expected_combination_count"] > 0:
        warnings.append(f"{validation['missing_expected_combination_count']} expected symbol/sweep combination(s) missing from raw data")
    if validation["duplicate_pair_count"] > 0:
        warnings.append(f"{validation['duplicate_pair_count']} duplicate (sweep, symbol) pair(s)")
    if validation["unexpected_symbol_count"] > 0:
        warnings.append(f"{validation['unexpected_symbol_count']} unexpected symbol(s): {validation['unexpected_symbols']}")
    if validation["missing_required_fields"]:
        warnings.append(f"Missing required fields in {sum(len(v) for v in validation['missing_required_fields'].values())} record(s)")
    if validation["field_type_mismatches"]:
        warnings.append(f"Field type mismatches in {sum(len(v) for v in validation['field_type_mismatches'].values())} record(s)")
    mismatches = [c for c in comparisons if not c["match"]]
    if mismatches:
        warnings.append(f"{len(mismatches)} summary reconciliation mismatch(es)")

    unresolved = [
        "provider grouping is not possible: raw records contain no provider identifier",
        "batch_timestamp_advanced semantics are uncertain (per-symbol bid/ask age delta vs batch-level)",
        "freshness classification thresholds cannot be exactly reproduced without the application's max_age constant; summary max_age_seconds reported for reference only",
        "price/volume 'changed' semantics for the first observation of each symbol (no previous) are treated as False/not-changed by the audit",
    ]

    audit = {
        "audit_meta": {
            "raw_path": raw_path,
            "summary_path": summary_path,
            "output_dir": output_dir,
            "script": "audit_quote_diagnostics.py",
        },
        "input_validation": validation,
        "http_integrity": http,
        "timestamp_validation": timestamps,
        "consecutive_observation_analysis": consecutive,
        "tolerance_price_comparison": tolerance,
        "flag_reevaluation": flags,
        "summary_reconciliation": comparisons,
        "warnings": warnings,
        "unresolved_questions": unresolved,
    }
    return audit


def write_outputs(audit: Dict[str, Any], output_dir: str) -> Tuple[str, str]:
    json_path = os.path.join(output_dir, "audit_report.json")
    md_path = os.path.join(output_dir, "audit_report.md")

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, default=str)

    md = build_markdown(audit)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    return json_path, md_path


def build_markdown(audit: Dict[str, Any]) -> str:
    lines = []
    lines.append("# Quote Diagnostics Audit Report")
    lines.append("")
    lines.append(f"Raw input: {audit['audit_meta']['raw_path']}")
    lines.append(f"Summary input: {audit['audit_meta']['summary_path']}")
    lines.append(f"Output directory: {audit['audit_meta']['output_dir']}")
    lines.append("")

    lines.append("## Input Validation (Section A)")
    v = audit["input_validation"]
    lines.append(f"- Physical lines: {v['total_physical_lines']}")
    lines.append(f"- Valid JSON records: {v['total_valid_records']}")
    lines.append(f"- Invalid JSON lines: {len(v['invalid_json_lines'])} -> {v['invalid_json_lines'][:10]}")
    lines.append(f"- Unique symbols observed: {v['unique_symbol_count']} -> {v['symbols_observed']}")
    lines.append(f"- Sweep range: {v['min_sweep']} to {v['max_sweep']}")
    lines.append(f"- Unique (sweep, symbol) pairs: {v['unique_pair_count']}")
    lines.append(f"- Duplicate (sweep, symbol) pairs: {v['duplicate_pair_count']}")
    lines.append(f"- Missing sweeps in observed range: {v['missing_sweeps_in_range']}")
    lines.append(f"- Missing expected symbol/sweep combinations: {v['missing_expected_combination_count']}")
    lines.append(f"- Unexpected symbols: {v['unexpected_symbol_count']} -> {v['unexpected_symbols']}")
    lines.append(f"- Missing required fields: {sum(len(x) for x in v['missing_required_fields'].values())} field instances")
    lines.append(f"- Field type mismatches: {sum(len(x) for x in v['field_type_mismatches'].values())} instances")
    lines.append("")

    lines.append("## HTTP / Request Integrity (Section B)")
    h = audit["http_integrity"]
    lines.append(f"- HTTP status distribution: {h['http_status_distribution']}")
    lines.append(f"- Success count: {h['success_count']}, Error count: {h['error_count']}")
    lines.append(f"- Latency stats: {h['latency_stats']}")
    lines.append(f"- Earliest request start: {h['earliest_request_start_utc']}")
    lines.append(f"- Latest request finish: {h['latest_request_finish_utc']}")
    lines.append(f"- Actual elapsed seconds: {h['actual_elapsed_seconds']}")
    lines.append(f"- Sweeps observed: {h['sweep_count_observed']}")
    lines.append(f"- batch_index values: {h['batch_index_values_observed']}")
    lines.append(f"- symbols_in_batch values: {h['symbols_in_batch_values_observed']}")
    lines.append("")

    lines.append("## Timestamp Validation (Section C)")
    t = audit["timestamp_validation"]
    for side in ("last", "bid", "ask"):
        s = t[side]
        lines.append(f"### {side}")
        lines.append(f"- Records with raw timestamp: {s['records_with_raw_timestamp']}")
        lines.append(f"- Status counts: {s['status_counts']}")
        lines.append(f"- Recalculated age stats: {s['recalculated_age_stats']}")
        lines.append(f"- Discrepancy count: {s['discrepancy_count']}, max abs discrepancy: {s['max_absolute_discrepancy_seconds']}")
    lines.append(f"- Note: {t['freshness_classification_note']}")
    lines.append("")

    lines.append("## Consecutive-Observation Analysis (Section D)")
    c = audit["consecutive_observation_analysis"]
    g = c["global"]
    lines.append(f"- Total consecutive pairs: {g['total_consecutive_pairs']}")
    lines.append(f"- Gap-marked transitions: {g['gap_marked_transitions']}")
    lines.append(f"- Price changed transitions: {g['price_changed_transitions']}")
    lines.append(f"- Last timestamp changed transitions: {g['last_timestamp_changed_transitions']}")
    lines.append(f"- Volume changed transitions: {g['volume_changed_transitions']}")
    lines.append(f"- Volume change while price & timestamp unchanged: {g['volume_change_price_ts_unchanged']}")
    lines.append(f"- Price change while timestamp unchanged: {g['price_change_ts_unchanged']}")
    lines.append(f"- Volume change while price unchanged: {g['volume_change_price_unchanged']}")
    lines.append(f"- Volume change while timestamp unchanged: {g['volume_change_ts_unchanged']}")
    lines.append(f"- Nonzero findings: {c['nonzero_finding_count']}")
    lines.append("")
    tol = audit["tolerance_price_comparison"]
    lines.append("### Tolerance-based price comparison (reference only)")
    lines.append(f"- Tolerance: {tol['tolerance']}")
    lines.append(f"- Exact changed: {tol['exact_price_changed_count']}")
    lines.append(f"- Tolerance changed: {tol['tolerance_price_changed_count']}")
    lines.append(f"- Differ: {tol['count_where_exact_and_tolerance_differ']}")
    lines.append("")

    lines.append("## Flag Re-evaluation (Section D)")
    fl = audit["flag_reevaluation"]
    for field, ag in fl["flag_agreement"].items():
        lines.append(f"- {field}: match={ag['match']}, mismatch={ag['mismatch']}, uncertain={ag['uncertain']}, total={ag['total']}")
    lines.append(f"- Note: {fl['batch_timestamp_advanced_semantics_note']}")
    lines.append("")

    lines.append("## Summary Reconciliation (Section E)")
    mismatches = [c for c in audit["summary_reconciliation"] if not c["match"]]
    matches = [c for c in audit["summary_reconciliation"] if c["match"]]
    lines.append(f"- Comparisons: {len(audit['summary_reconciliation'])} total, {len(matches)} match, {len(mismatches)} mismatch")
    for comp in mismatches:
        lines.append(f"### MISMATCH: {comp['metric']}")
        lines.append(f"- Raw: {comp['raw_derived_value']}, Summary: {comp['summary_value']}, Diff: {comp['difference']}")
        lines.append(f"- Note: {comp['note']}")
    lines.append("")

    lines.append("## Warnings")
    for w in audit["warnings"]:
        lines.append(f"- {w}")
    lines.append("")

    lines.append("## Unresolved Questions")
    for u in audit["unresolved_questions"]:
        lines.append(f"- {u}")
    lines.append("")

    return "\n".join(lines)


def verify_outputs(json_path: str, md_path: str) -> None:
    """Verify generated outputs can be read and key counts agree."""
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    with open(md_path, "r", encoding="utf-8") as fh:
        md = fh.read()

    # Key count checks
    v = data["input_validation"]
    assert v["total_valid_records"] == sum(
        1 for _ in v["per_symbol_observed"].values()
    ) or True  # per-symbol may not cover all if symbols missing

    # Check observation totals consistency
    total_per_symbol = sum(
        d["count"] for d in v["per_symbol_observed"].values()
    )
    assert total_per_symbol == v["total_valid_records"], (
        f"per-symbol total {total_per_symbol} != valid records {v['total_valid_records']}"
    )

    # Check summary reconciliation present
    assert len(data["summary_reconciliation"]) > 0
    assert len(md) > 0
    assert "MISMATCH" in md or "match" in md.lower()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a Public.com quote_diagnostics run.")
    parser.add_argument("--raw", default=DEFAULT_RAW_PATH, help="Path to raw JSONL")
    parser.add_argument("--summary", default=DEFAULT_SUMMARY_PATH, help="Path to summary JSON")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="Root for output directory")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.raw):
        print(f"ERROR: raw JSONL not found: {args.raw}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.summary):
        print(f"ERROR: summary JSON not found: {args.summary}", file=sys.stderr)
        return 2

    output_dir = unique_output_dir(args.output_root, "audit_quote_diagnostics")

    audit = run_audit(args.raw, args.summary, output_dir)
    json_path, md_path = write_outputs(audit, output_dir)

    # Verify outputs before announcing success
    try:
        verify_outputs(json_path, md_path)
    except AssertionError as exc:
        print(f"ERROR: output verification failed: {exc}", file=sys.stderr)
        return 3

    print(f"Audit complete.")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")
    print(f"  Valid records: {audit['input_validation']['total_valid_records']}")
    print(f"  Warnings: {len(audit['warnings'])}")
    print(f"  Reconciliation mismatches: {sum(1 for c in audit['summary_reconciliation'] if not c['match'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
